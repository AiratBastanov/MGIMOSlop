"""S5: personal application events are independent of academic source validity.

All fixtures and the source-validity clock probe use the isolated
PostgreSQL test database. Runtime clocks and accepted working r9 are unchanged.
"""
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.db import IntegrityError, transaction
from django.test import Client, RequestFactory, TransactionTestCase
from django.utils import timezone

from accounts.access import AccessDenied, current_actor
from accounts.models import Identity, SessionContext, StaffScope
from accounts.provisioning import provision
from accounts.selectors import private_snapshot
from data_import.contracts import content_hash, validate
from data_import.models import Dataset
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply, initialize
from data_import.state import canonical_state
from notifications.actions import mark_read
from notifications.models import Notification, NotificationRead
from student_requests.models import Request, RequestEvent
from student_requests.services import issue_submission, submit_request, transition_request
from student_requests.workflow import REQUEST_TYPE, READY_NOTICE


ROOT = Path(__file__).resolve().parents[1]


class S5NotificationTests(TransactionTestCase):
    def setUp(self):
        initialize()
        self.provider = SyntheticDatasetProvider()
        for revision in range(1, 10):
            stage = 's1' if revision < 3 else 's2' if revision < 5 else 's3' if revision < 8 else 's4'
            apply(self.provider.load(ROOT / f'fixtures/{stage}/r{revision}.json'),
                  expected_revision=revision - 1, operator='isolated-s5-notifications')
        directory = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.addCleanup(directory.cleanup)
        self.credentials_path = Path(directory.name) / 'credentials.json'
        provision(self.credentials_path)
        self.credentials = json.loads(self.credentials_path.read_text(encoding='utf-8'))
        self.a, self.b, self.staff = (Client(enforce_csrf_checks=True) for _ in range(3))
        for client, username in ((self.a, 'student_a'), (self.b, 'student_b'), (self.staff, 'staff_demo')):
            self.login(client, username)

    def login(self, client, username):
        client.get('/login/')
        response = client.post('/login/', {'username': username, 'password': self.credentials[username],
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
        self.assertEqual(response.status_code, 302)

    def request(self, client=None):
        request = RequestFactory().post('/requests/new/')
        request.session = SessionStore(session_key=(client or self.a).cookies['sessionid'].value)
        dict(request.session.items())
        return request

    def submit(self, client=None, comment='Для демонстрации'):
        request = self.request(client)
        actor = current_actor(request)
        result = submit_request(request, actor.marker, issue_submission(actor), REQUEST_TYPE, comment)
        return Request.objects.get(pk=result['id'])

    def transition(self, row, action, explanation=''):
        request = self.request(self.staff)
        result = transition_request(request, row.pk, current_actor(request).marker,
                                    action, row.version, explanation)
        row.refresh_from_db()
        return result

    def snapshot(self, client=None, **kwargs):
        return private_snapshot(self.request(client), section='notifications', notification_kind='requests', **kwargs)

    def read(self, event, client=None):
        request = self.request(client)
        return mark_read(request, event.pk, current_actor(request).marker)

    def test_current_terminal_and_scoped_history_preserve_old_identity_and_read(self):
        baseline_unread = self.snapshot()['unread']
        row = self.submit()
        first = Notification.objects.get(request_event__request=row)
        original = dict(Notification.objects.filter(pk=first.pk).values().get())
        page = self.snapshot()
        self.assertEqual(page['unread'], baseline_unread + 1)
        self.assertEqual(page['notification_count'], 1)
        self.assertEqual(page['notifications'][0]['event_status_label'], 'Принята')
        self.assertEqual(NotificationRead.objects.count(), 0)
        self.assertTrue(self.read(first)['created'])
        self.assertFalse(self.read(first)['created'])
        saved_read = list(NotificationRead.objects.values())
        self.transition(row, 'take')
        self.transition(row, 'complete', 'Синтетический результат: проверка завершена.')
        self.assertEqual((row.status, row.version), ('ready', 3))
        active = self.snapshot()
        self.assertEqual(active['notification_count'], 1)
        self.assertEqual(active['unread'], baseline_unread + 1)
        item = active['notifications'][0]
        self.assertEqual(item['event_status_label'], 'Готова')
        self.assertTrue(item['can_read'])
        self.assertEqual(item['ready_notice'], READY_NOTICE)
        self.assertEqual(item['explanation'], 'Синтетический результат: проверка завершена.')
        history = self.snapshot(archive=True)['notifications']
        self.assertEqual(len(history), 3)
        old = next(x for x in history if x['id'] == first.pk)
        self.assertEqual(old['event_status_label'], 'Принята')
        self.assertEqual(old['current']['status_label'], 'Готова')
        self.assertTrue(old['is_historical'])
        self.assertTrue(old['is_read'])
        self.assertFalse(old['can_read'])
        with self.assertRaises(AccessDenied) as denied:
            self.read(first)
        self.assertEqual(denied.exception.status, 404)
        self.assertEqual(Notification.objects.filter(pk=first.pk).values().get(), original)
        self.assertEqual(list(NotificationRead.objects.values()), saved_read)
        self.assertEqual(RequestEvent.objects.filter(request=row).count(), 3)
        self.assertEqual(Notification.objects.filter(request_event__request=row).count(), 3)
        self.read(Notification.objects.get(pk=item['id']))
        self.assertEqual(self.snapshot()['unread'], baseline_unread)
        row.refresh_from_db()
        self.assertEqual((row.status, row.version), ('ready', 3))

    def test_owner_scope_precedes_filters_counts_history_read_and_link(self):
        a = self.submit(comment='Только заявитель А')
        b = self.submit(self.b, comment='Только заявитель Б')
        self.transition(a, 'reject', 'Демонстрационная причина отказа А')
        a_events = set(Notification.objects.filter(request_event__request=a).values_list('pk', flat=True))
        b_event = Notification.objects.get(request_event__request=b)
        self.assertEqual(self.snapshot()['notification_count'], 1)
        self.assertEqual(self.snapshot(self.b)['notification_count'], 1)
        self.assertEqual(len(self.snapshot(archive=True)['notifications']), 2)
        self.assertEqual(len(self.snapshot(self.b, archive=True)['notifications']), 1)
        for event_id in a_events:
            response = self.b.get(f'/notifications/{event_id}/')
            self.assertEqual(response.status_code, 404)
            with self.assertRaises(AccessDenied) as denied:
                self.read(Notification.objects.get(pk=event_id), self.b)
            self.assertEqual(denied.exception.status, 404)
        self.assertEqual(self.a.get(f'/notifications/{b_event.pk}/').status_code, 404)
        self.assertEqual(self.b.get(f'/requests/{a.pk}/').status_code, 404)
        self.assertEqual(self.staff.get(f'/notifications/{b_event.pk}/').status_code, 403)
        self.assertEqual(Client().get(f'/notifications/{b_event.pk}/').status_code, 302)
        self.assertEqual(NotificationRead.objects.count(), 0)
        own = self.a.get('/notifications/?kind=requests&archive=1')
        self.assertEqual(own.status_code, 200)
        self.assertNotContains(own, 'Только заявитель Б')
        self.assertEqual(own.context['notification_count'], 2)

    def test_partial_then_expired_academic_source_does_not_hide_or_block_application_events(self):
        row = self.submit()
        source = json.loads((ROOT / 'fixtures/s4/r9.json').read_text(encoding='utf-8'))
        source.update(dataset_revision=10, parent_revision=9, source_as_of=timezone.now().isoformat(), operations=[])
        for coverage in source['academic_coverage']:
            coverage.update(completeness='partial', record_ids=None, record_count=None)
        expires = datetime.fromisoformat(source['valid_until'])
        source['content_sha256'] = content_hash(source)
        apply(validate(source), expected_revision=9, operator='isolated-s5-partial-validity')
        self.assertEqual(self.snapshot()['notification_count'], 1)
        self.assertEqual(private_snapshot(self.request(), section='requests')['request_count'], 1)
        # Explicit isolated clock probe after the unchanged academic validity.
        # New sessions authenticate at that simulated server time; their real
        # two-hour rule is not bypassed or extended to keep an old session alive.
        with patch('django.utils.timezone.now', return_value=expires + timedelta(seconds=10)):
            self.a, self.staff = (Client(enforce_csrf_checks=True) for _ in range(2))
            self.login(self.a, 'student_a')
            self.login(self.staff, 'staff_demo')
            current = self.snapshot()
            self.assertEqual(current['notification_count'], 1)
            self.assertEqual(current['unread'], 1)
            self.assertTrue(current['notifications'][0]['can_read'])
            self.assertEqual(current['notifications'][0]['source_message'], '')
            academic = private_snapshot(self.request(), section='notifications', notification_kind='academic')
            self.assertEqual(academic['notification_count'], 0)
            self.transition(row, 'take')
            another = self.submit(comment='Подано после окончания учебного источника')
            self.assertEqual(self.snapshot()['notification_count'], 2)
            latest = Notification.objects.get(request_event__request=row, request_event__version=2)
            self.assertTrue(self.read(latest)['created'])
            self.assertEqual(self.snapshot()['unread'], 1)
            self.assertEqual(private_snapshot(self.request(), section='request_detail', request_id=another.pk)
                             ['request_item']['comment'], 'Подано после окончания учебного источника')

    def test_request_activity_replay_and_provision_keep_r9_hashes_security_and_legacy_reads(self):
        legacy = Notification.objects.filter(request_event__isnull=True,
            schedule_change__lesson__group_id=current_actor(self.request()).group_id).first()
        self.assertIsNotNone(legacy)
        self.read(legacy)
        accepted = canonical_state()
        head = list(Dataset.objects.values())
        security = {
            'users': list(get_user_model().objects.order_by('pk').values()),
            'identity': list(Identity.objects.order_by('pk').values()),
            'scope': list(StaffScope.objects.order_by('pk').values()),
            'contexts': list(SessionContext.objects.order_by('pk').values()),
        }
        row = self.submit()
        self.transition(row, 'take')
        self.transition(row, 'complete', 'Демонстрационная обработка завершена.')
        notification = Notification.objects.get(request_event__request=row, request_event__version=3)
        self.read(notification)
        requests = list(Request.objects.values())
        events = list(RequestEvent.objects.values())
        notifications = list(Notification.objects.order_by('pk').values())
        reads = list(NotificationRead.objects.order_by('pk').values())
        self.assertEqual(canonical_state(), accepted)
        self.assertEqual(list(Dataset.objects.values()), head)
        self.assertEqual(initialize()['revision'], 9)
        self.assertEqual(apply(self.provider.load(ROOT / 'fixtures/s4/r9.json'), expected_revision=9,
                               operator='isolated-s5-replay')['outcome'], 'noop')
        self.assertEqual(provision(self.credentials_path), [])
        self.assertEqual(canonical_state(), accepted)
        self.assertEqual(list(Request.objects.values()), requests)
        self.assertEqual(list(RequestEvent.objects.values()), events)
        self.assertEqual(list(Notification.objects.order_by('pk').values()), notifications)
        self.assertEqual(list(NotificationRead.objects.order_by('pk').values()), reads)
        self.assertEqual(list(get_user_model().objects.order_by('pk').values()), security['users'])
        self.assertEqual(list(Identity.objects.order_by('pk').values()), security['identity'])
        self.assertEqual(list(StaffScope.objects.order_by('pk').values()), security['scope'])
        self.assertEqual(list(SessionContext.objects.order_by('pk').values()), security['contexts'])

    def test_required_typed_target_uniqueness_and_notification_immutability(self):
        row = self.submit()
        notification = Notification.objects.get(request_event__request=row)
        event = notification.request_event
        self.assertEqual(notification.published_at, event.recorded_at)
        for values in (
            {'request_event': event, 'published_at': event.recorded_at},
            {'published_at': event.recorded_at},
            {'request_event': event, 'group_id': current_actor(self.request()).group_id,
             'published_at': event.recorded_at},
        ):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Notification.objects.create(**values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.filter(pk=notification.pk).update(published_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.filter(pk=notification.pk).delete()
        self.assertEqual(Notification.objects.filter(request_event__request=row).count(), 1)

    def test_plain_text_decisions_escaped_and_old_status_labelled_historical(self):
        row = self.submit(comment='<script>alert("draft")</script>')
        self.transition(row, 'reject', '<img src=x onerror="alert(1)"> Причина <отказа>')
        response = self.a.get('/notifications/?kind=requests&archive=1')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;')
        self.assertNotContains(response, '<img src=x')
        self.assertContains(response, 'Причина отказа')
        self.assertContains(response, 'На момент события: Принята')
        self.assertContains(response, 'Сейчас заявка: Отклонена')
        self.assertEqual(NotificationRead.objects.count(), 0)
