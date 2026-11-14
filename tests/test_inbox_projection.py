"""Проекции страницы, точный снимок операций и прежняя группировка уведомлений."""
from datetime import timedelta
from unittest.mock import patch
from django.test import TransactionTestCase
from django.utils import timezone
from data_import.services import read_snapshot
from accounts.selectors import private_snapshot
from notifications.models import NotificationRead, NotificationReceipt
from notifications.selectors import inbox_page, inbox_unread, presentation_events, thread_events
from studies.selectors import schedule_snapshot
from tests import test_task10_notifications as fixtures


class InboxProjectionTests(TransactionTestCase):
    setUp=fixtures.NotificationWorkflowTests.setUp
    login=fixtures.NotificationWorkflowTests.login
    request=fixtures.NotificationWorkflowTests.request
    marker=fixtures.NotificationWorkflowTests.marker
    actor=fixtures.NotificationWorkflowTests.actor
    start=fixtures.NotificationWorkflowTests.start
    create=fixtures.NotificationWorkflowTests.create
    transition=fixtures.NotificationWorkflowTests.transition

    def test_requests_keep_source_dates_without_course_or_lesson_projection(self):
        actor=self.start();self.create();now=timezone.now()
        with read_snapshot():
            full=schedule_snapshot(actor,now)
            source=schedule_snapshot(actor,now,source_only=True)
        self.assertEqual(full['source'],source['source'])
        self.assertEqual(source['lessons'],[]);self.assertEqual(source['history'],[])
        with patch('accounts.selectors.academic_snapshot',side_effect=AssertionError('Учебная проекция не нужна')):
            with patch('accounts.selectors.schedule_snapshot',wraps=schedule_snapshot) as schedule:
                data=private_snapshot(self.request(),section='notifications',notification_kind='requests')
        self.assertTrue(data['notifications']);self.assertTrue(schedule.call_args.kwargs['source_only'])
        self.assertIn('source_as_of',data['source'])

    def test_history_no_read_all_token_and_unread_remains_exact(self):
        actor=self.start();_,event=self.create();now=timezone.now()
        NotificationReceipt.objects.filter(user_id=actor.user_id,notification=event).update(available_at=now-timedelta(days=8))
        history=inbox_page(actor,now=now,mode='history')
        self.assertEqual(history['read_all_token'],'')
        self.assertEqual(history['unread'],inbox_unread(actor,now))
        self.assertIn(event.pk,[e.pk for e in history['events']])
        inbox=inbox_page(actor,now=now)
        self.assertTrue(inbox['read_all_token'])
        self.assertEqual(inbox['unread'],inbox_unread(actor,now))

    def test_inbox_single_grouping_matches_legacy_with_reads_dismissal_and_late_event(self):
        actor=self.start();row,first=self.create();latest=self.transition(row,'take')
        _,other=self.create();now=timezone.now()
        NotificationRead.objects.create(user_id=actor.user_id,notification=other)
        NotificationReceipt.objects.filter(user_id=actor.user_id,notification=latest).update(dismissed_at=now)
        for mode in ('inbox','history'):
            legacy=list(thread_events(presentation_events(actor,now=now,mode=mode),actor).order_by('-available_at','-published_at','-pk').values_list('pk',flat=True))
            actual=inbox_page(actor,now=now,mode=mode)
            self.assertEqual([e.pk for e in actual['events']],legacy[:10])
            self.assertEqual(actual['notification_count'],len(legacy))
            self.assertNotIn(first.pk,[e.pk for e in actual['events']] if mode=='inbox' else [])
        self.assertEqual(inbox_page(actor,now=now)['unread'],inbox_unread(actor,now))
