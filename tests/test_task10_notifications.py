"""TASK10: семь суток, персональная очистка, треды и локальная почта."""
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import Client, TransactionTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.access import AccessDenied, current_actor, logout_session
from data_import.services import apply
from notifications.delivery import deliver_pending, register, sync_recipient
from notifications.inbox_actions import clear_inbox, undo_clear
from notifications.models import Notification, NotificationReceipt, NotificationRead, EmailOutbox
from notifications.selectors import inbox_events, inbox_page, inbox_unread
from profiles.forms import ProfileForm
from profiles.models import Preferences
from profiles.services import update_profile, verify_email
from student_requests.models import Request
from student_requests.services import issue_submission, submit_request, transition_request
from tests import test_auth


class NotificationWorkflowTests(TransactionTestCase):
    setUp = test_auth.AuthTests.setUp
    login = test_auth.AuthTests.login
    request = test_auth.AuthTests.request
    page = test_auth.AuthTests.page
    marker = test_auth.AuthTests.marker
    scope_change = test_auth.AuthTests.scope_change

    def actor(self, client=None):
        return current_actor(self.request(client))

    def start(self):
        self.login()
        return self.actor()

    def create(self, client=None):
        req = self.request(client)
        actor = current_actor(req)
        result = submit_request(req, actor.marker, issue_submission(actor), 'study_certificate', 'Изолированная проверка')
        row = Request.objects.get(pk=result['id'])
        event = Notification.objects.get(request_event__request=row)
        return row, event

    def transition(self, row, action, text=''):
        if not hasattr(self, 'staff'):
            self.staff = Client(enforce_csrf_checks=True)
            self.login(self.staff, 'staff_demo')
        req = self.request(self.staff)
        actor = current_actor(req)
        transition_request(req, row.pk, actor.marker, action, row.version, text)
        row.refresh_from_db()
        return Notification.objects.get(request_event__request=row, request_event__version=row.version)

    def preferences(self, **changes):
        actor = self.actor()
        pref = Preferences.objects.filter(user_id=actor.user_id).first()
        data = {'version': pref.version if pref else 0,
                'contact_email': pref.contact_email if pref else 'student@example.invalid',
                'email_enabled': True, 'email_requests': True, 'email_academic': True,
                'email_announcements': True, 'photo_audience': 'private', **changes}
        form = ProfileForm(data, instance=pref)
        self.assertTrue(form.is_valid(), form.errors)
        return update_profile(self.request(), actor.marker, form.cleaned_data)

    def mail_settings(self):
        return override_settings(EMAIL_BACKEND='notifications.mail_backend.CaptureBackend',
            EMAIL_FILE_PATH=str(Path(self.temp.name) / 'captured'),
            PUBLIC_BASE_URL='http://127.0.0.1:8000', MAIL_EXTERNAL_ENABLED=False)

    def verify_address(self):
        pref = self.preferences()
        result = deliver_pending()
        self.assertEqual(result['captured'], 1)
        paths = list((Path(self.temp.name) / 'captured').glob('*.eml'))
        message = BytesParser(policy=policy.default).parsebytes(paths[-1].read_bytes())
        link = next(line for line in message.get_content().splitlines() if '/profile/verify-email/' in line)
        token = parse_qs(urlsplit(link).query)['token'][0]
        verify_email(self.request(), self.actor().marker, token)
        pref.refresh_from_db()
        self.assertIsNotNone(pref.email_verified_at)
        return pref, token

    def test_receipt_exact_seven_day_boundary_and_newly_available_old_source(self):
        actor = self.start()
        _, notification = self.create()
        now = timezone.now().astimezone(ZoneInfo('Europe/Moscow'))
        receipt = NotificationReceipt.objects.get(user_id=actor.user_id, notification=notification)
        NotificationReceipt.objects.filter(pk=receipt.pk).update(available_at=now-timedelta(days=7))
        self.assertFalse(inbox_events(actor, now=now).filter(pk=notification.pk).exists())
        self.assertTrue(inbox_events(actor, now=now-timedelta(microseconds=1)).filter(pk=notification.pk).exists())
        self.assertFalse(inbox_events(actor, now=now.astimezone(ZoneInfo('UTC'))).filter(pk=notification.pk).exists())
        # Старое событие источника впервые доступно получателю сейчас.
        NotificationReceipt.objects.filter(user_id=actor.user_id, notification=self.notification).delete()
        register(self.notification, actor.user_id, now, queue_email=False)
        fresh = NotificationReceipt.objects.get(user_id=actor.user_id, notification=self.notification)
        self.assertEqual(fresh.available_at, now)
        self.assertGreater(fresh.available_at, self.notification.published_at)

    def test_thread_grouping_sql_ten_page_and_latest_unread(self):
        actor = self.start()
        baseline_unread = inbox_unread(actor, timezone.now())
        ids = []
        for _ in range(12):
            row, _ = self.create()
            self.transition(row, 'take')
            latest = self.transition(row, 'complete', 'Демонстрационный ответ')
            ids.append(latest.pk)
        now = timezone.now()
        with CaptureQueriesContext(connection) as captured:
            first = inbox_page(actor, now=now, kind='requests')
        second = inbox_page(actor, now=now, kind='requests', page_number=2)
        self.assertEqual(first['notification_count'], 12)
        self.assertEqual(len(first['events']), 10)
        self.assertEqual(len(second['events']), 2)
        self.assertEqual({event.pk for event in first['events'] + second['events']}, set(ids))
        self.assertTrue(any('LIMIT 10' in query['sql'] for query in captured.captured_queries))
        archive = inbox_page(actor, now=now, kind='requests', archive=True)
        self.assertEqual(archive['notification_count'], 12)  # 36 переходов, 12 тредов.
        NotificationRead.objects.create(user_id=actor.user_id, notification_id=ids[-1])
        self.assertEqual(inbox_unread(actor, now), baseline_unread + 11)
        self.assertIn('kind=requests', first['page_next_url'])

    def test_clear_cutoff_preserves_new_event_and_next_update_reopens_thread(self):
        actor = self.start()
        old_request, old = self.create()
        sync_recipient(actor.user_id)
        token = inbox_page(actor, now=timezone.now(), kind='requests')['clear_token']
        _, arriving = self.create()
        count = Notification.objects.count()
        batch = clear_inbox(self.request(), actor.marker, token)
        self.assertEqual(Notification.objects.count(), count)
        self.assertTrue(inbox_events(actor, now=timezone.now()).filter(pk=arriving.pk).exists())
        self.assertFalse(inbox_events(actor, now=timezone.now()).filter(pk=old.pk).exists())
        self.assertTrue(NotificationReceipt.objects.filter(user_id=actor.user_id,
            notification=self.notification, clear_batch=batch).exists())  # Очистка всех категорий.
        self.assertEqual(NotificationRead.objects.count(), 0)
        updated = self.transition(old_request, 'take')
        self.assertTrue(inbox_events(actor, now=timezone.now()).filter(pk=updated.pk).exists())
        self.assertEqual(old_request.events.count(), 2)

    def test_undo_does_not_restore_expired_out_of_scope_or_other_users_entries(self):
        actor = self.start()
        _, current = self.create()
        _, expiring = self.create()
        sync_recipient(actor.user_id)
        now = timezone.now()
        NotificationReceipt.objects.filter(user_id=actor.user_id, notification=expiring).update(
            available_at=now-timedelta(days=7)+timedelta(seconds=30))
        token = inbox_page(actor, now=now)['clear_token']
        batch = clear_inbox(self.request(), actor.marker, token)
        self.login(self.b, 'student_b')
        with self.assertRaises(AccessDenied):
            undo_clear(self.request(self.b), self.actor(self.b).marker, batch)
        apply(self.scope_change(), expected_revision=4, operator='task10-clear-scope')
        with patch('django.utils.timezone.now', return_value=now+timedelta(minutes=1)):
            actor = self.actor()
            undo_clear(self.request(), actor.marker, batch)
            self.assertTrue(inbox_events(actor, now=actor.checked_at).filter(pk=current.pk).exists())
            self.assertFalse(inbox_events(actor, now=actor.checked_at).filter(pk=expiring.pk).exists())
        self.assertIsNotNone(NotificationReceipt.objects.get(user_id=actor.user_id,
            notification=self.notification).dismissed_at)
        # TASK11 makes an authorized exact retry idempotent after a lost response.
        undo_clear(self.request(), actor.marker, batch)

    def test_outbox_capture_verify_retry_and_minimal_message(self):
        self.start()
        with self.mail_settings():
            pref, _ = self.verify_address()
            self.create()
            row = EmailOutbox.objects.get(purpose='notification')
            with patch('django.core.mail.EmailMessage.send', side_effect=OSError('private SMTP text')):
                failed = deliver_pending()
            row.refresh_from_db()
            self.assertEqual(failed['failed'], 1)
            self.assertEqual(row.attempts, 1)
            self.assertEqual(row.error_code, 'OSError')
            self.assertGreater(row.next_attempt_at, row.attempted_at)
            EmailOutbox.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now()-timedelta(seconds=1))
            accepted = deliver_pending()
            row.refresh_from_db()
            self.assertEqual(accepted['captured'], 1)
            self.assertEqual(row.status, 'captured')
            self.assertEqual(row.attempts, 2)
            self.assertIsNone(row.confirmed_at)
            self.assertEqual(deliver_pending()['attempted'], 0)
            messages = [BytesParser(policy=policy.default).parsebytes(path.read_bytes())
                        for path in (Path(self.temp.name) / 'captured').glob('*.eml')]
            body = next(message.get_content() for message in messages if message['Subject'] == 'Новое уведомление в кабинете')
            self.assertIn('/notifications/', body)
            self.assertNotIn('Изолированная проверка', body)
            self.assertNotIn('Алина', body)
            self.assertEqual(EmailOutbox.objects.filter(purpose='verification', confirmed_at__isnull=False).count(), 1)

    def test_outbox_cancels_after_address_preferences_or_eligibility_change(self):
        actor = self.start()
        with self.mail_settings():
            pref, old_token = self.verify_address()
            _, notification = self.create()
            self.preferences(contact_email='changed@example.invalid')
            result = deliver_pending()
            self.assertEqual(result['cancelled'], 1)
            self.assertEqual(EmailOutbox.objects.get(notification=notification).status, 'cancelled')
            with self.assertRaises(AccessDenied):
                verify_email(self.request(), actor.marker, old_token)
            pref = Preferences.objects.get(user_id=actor.user_id)
            pref.email_verified_at = timezone.now()
            pref.save(update_fields=['email_verified_at'])
            _, next_event = self.create()
            self.preferences(email_enabled=False)
            self.assertEqual(deliver_pending()['cancelled'], 1)
            self.assertEqual(EmailOutbox.objects.get(notification=next_event).status, 'cancelled')
            self.preferences(email_enabled=True, email_requests=False)
            _, opted_out = self.create()
            self.assertFalse(EmailOutbox.objects.filter(notification=opted_out).exists())
            self.preferences(email_requests=True)
            _, clear_event = self.create()
            token = inbox_page(actor, now=timezone.now())['clear_token']
            clear_inbox(self.request(), actor.marker, token)
            self.assertEqual(deliver_pending()['cancelled'], 1)
            self.assertEqual(EmailOutbox.objects.get(notification=clear_event).status, 'cancelled')

    def test_outbox_failure_rolls_back_event_and_external_gate_never_sends(self):
        actor = self.start()
        Preferences.objects.create(user_id=actor.user_id, contact_email='synthetic@example.invalid',
            email_enabled=True, email_verified_at=timezone.now())
        before = (Request.objects.count(), Notification.objects.count(), NotificationReceipt.objects.count())
        with patch('notifications.delivery.EmailOutbox.objects.get_or_create', side_effect=RuntimeError('outbox failure')):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertEqual((Request.objects.count(), Notification.objects.count(), NotificationReceipt.objects.count()), before)
        self.create()
        with override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend', MAIL_EXTERNAL_ENABLED=False), \
                patch('django.core.mail.EmailMessage.send') as send:
            result = deliver_pending()
        self.assertEqual(result['failed'], 1)
        send.assert_not_called()

    def test_stale_session_cannot_clear_or_confirm_address(self):
        actor = self.start()
        self.create()
        token = inbox_page(actor, now=timezone.now())['clear_token']
        logout_session(self.request(), actor.marker)
        self.login()
        with self.assertRaises(AccessDenied):
            clear_inbox(self.request(), self.actor().marker, token)
        self.assertFalse(NotificationReceipt.objects.filter(dismissed_at__isnull=False).exists())
