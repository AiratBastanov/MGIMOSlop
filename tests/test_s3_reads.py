"""S3: scoped страницы, граничные часы и реальные соединения PostgreSQL.

Future clock применяется только к read_now в изолированной test DB; серверное
время обычного приложения и абсолютный срок сессии не изменяются.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, connections, OperationalError
from django.test import Client, RequestFactory, TransactionTestCase
from django.utils import timezone

from accounts.access import AccessDenied
from accounts.models import AuthorizationGuard
from accounts.provisioning import provision
from accounts.selectors import private_snapshot
from accounts.views import safe_next
from data_import.contracts import content_hash, validate
from data_import.models import Dataset, ImportAttempt
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply, initialize
from data_import.state import records
from notifications.actions import mark_read
from notifications.models import Announcement, Notification, NotificationRead
from studies.models import Lesson, StudentProfile

ROOT = Path(__file__).resolve().parents[1]


class S3ReadTests(TransactionTestCase):
    def setUp(self):
        initialize()
        provider = SyntheticDatasetProvider()
        for revision in range(1,8):
            folder = 's1' if revision < 3 else ('s2' if revision < 5 else 's3')
            apply(provider.load(ROOT / f'fixtures/{folder}/r{revision}.json'),
                  expected_revision=revision-1,operator='isolated-s3-read-tests')
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'.local')
        self.addCleanup(self.temp.cleanup)
        credential_path = Path(self.temp.name)/'demo.json'
        provision(credential_path)
        self.credentials = json.loads(credential_path.read_text(encoding='utf-8'))
        self.a,self.b,self.staff = (Client(enforce_csrf_checks=True) for _ in range(3))
        for client,username in ((self.a,'student_a'),(self.b,'student_b'),(self.staff,'staff_demo')):
            self.login(client,username)
        self.expected = json.loads((ROOT/'fixtures/s3/expectations.json').read_text(encoding='utf-8'))

    def login(self, client, username):
        client.get('/login/')
        response = client.post('/login/',{'username':username,'password':self.credentials[username],
            'csrfmiddlewaretoken':client.cookies['csrftoken'].value})
        self.assertEqual(response.status_code,302)
        self.assertEqual(response.url,'/staff/' if username == 'staff_demo' else '/today/')

    def request(self, client=None):
        request = RequestFactory().get('/today/')
        request.session = SessionStore(session_key=(client or self.a).cookies['sessionid'].value)
        dict(request.session.items())
        return request

    def snapshot(self, *, section='today', client=None, when='2026-11-07T23:30:00+03:00', **kwargs):
        with patch('accounts.selectors.read_now',return_value=datetime.fromisoformat(when)):
            return private_snapshot(self.request(client),section=section,**kwargs)

    def candidate(self, *, operations=None, coverage=None):
        payload = json.loads((ROOT/'fixtures/s3/r7.json').read_text(encoding='utf-8'))
        revision = Dataset.objects.get().current_revision
        payload.update(dataset_revision=revision+1,parent_revision=revision,operations=operations or [],
                       source_as_of=timezone.now().isoformat(),preparation_reason='Изолированный S3 test; не рабочие данные')
        if coverage is not None:
            payload['coverage'] = coverage
        payload['content_sha256'] = content_hash(payload)
        return validate(payload)

    def import_candidate(self, batch):
        return apply(batch,expected_revision=batch.parent,operator='isolated-s3-read-tests')

    def test_independent_day_expectations_for_both_groups_and_no_double_count(self):
        for group,client in (('G-MB21',self.a),('G-EK11',self.b)):
            for chosen,expected_ids in self.expected['active_days'][group].items():
                with self.subTest(group=group,date=chosen):
                    data = self.snapshot(section='schedule',client=client,selected_date=date.fromisoformat(chosen))
                    day = data['days'][0]
                    self.assertEqual([row['source_id'] for row in day['lessons']],expected_ids)
                    if chosen == '2026-12-07':
                        self.assertEqual(day['state'],'outside')
                        self.assertIsNone(day['total'])
                        self.assertNotIn('занятий нет',day['message'])
                    else:
                        self.assertEqual(day['total'],len(expected_ids))
                    if chosen == '2026-11-08':
                        self.assertEqual(day['state'],'complete_empty')
        old_day = self.snapshot(section='schedule',selected_date=date(2026,11,23))['days'][0]
        marker = next(x for x in old_day['markers'] if x['current']['source_id'] == 'L-A-1123-1')
        self.assertEqual(marker['before']['date'],date(2026,11,23))
        self.assertEqual(marker['after']['date'],date(2026,11,24))
        self.assertEqual(old_day['total'],2)

    def test_today_week_day_detail_and_notifications_share_one_current_fact_and_history(self):
        lesson = Lesson.objects.get(source_id='L-A-1123-1')
        moment = '2026-11-24T13:59:00+03:00'
        today = self.snapshot(when=moment)
        detail = self.snapshot(section='lesson',lesson_id=lesson.pk,when=moment)
        week = self.snapshot(section='schedule',selected_date=date(2026,11,24),mode='week',when=moment)
        feed = self.snapshot(section='notifications',when=moment)
        expected = detail['lesson']
        self.assertEqual(today['spotlight']['kind'],'next')
        self.assertEqual(today['spotlight']['lesson'],expected)
        self.assertEqual(next(x for x in today['day']['lessons'] if x['id'] == lesson.pk),expected)
        self.assertEqual(next(x for day in week['days'] for x in day['lessons'] if x['id'] == lesson.pk),expected)
        room_event = next(x for x in feed['notifications'] if x['event_id']=='EV-A-ROOM')
        move_event = next(x for x in feed['notifications'] if x['event_id']=='EV-A-ROOM-THEN-MOVE')
        self.assertEqual(room_event['current'],expected)
        self.assertEqual(move_event['current'],expected)
        today_ids = [item['current']['id'] for item in today['changes']]
        self.assertEqual(len(today_ids),len(set(today_ids)))
        self.assertLessEqual(len(today_ids),3)
        current_move = [item for item in today['changes'] if item['current']['source_id']=='L-A-1123-1']
        self.assertEqual([item['event_id'] for item in current_move],['EV-A-ROOM-THEN-MOVE'])
        self.assertNotIn('EV-A-ROOM',{item['event_id'] for item in today['changes']})
        self.assertTrue(room_event['is_historical'])
        self.assertFalse(move_event['is_historical'])
        self.assertEqual([x['event_id'] for x in detail['history']],['EV-A-ROOM','EV-A-ROOM-THEN-MOVE'])
        self.assertEqual((expected['date'],expected['time_label'],expected['room']),
                         (date(2026,11,24),'14:00–15:30','318'))
        self.assertEqual((room_event['before']['room'],room_event['after']['room']),('201','318'))
        self.assertEqual((move_event['before']['date'],move_event['after']['date']),
                         (date(2026,11,23),date(2026,11,24)))
        s2 = next(x for x in feed['notifications'] if x['event_id']=='EV-S2-ROOM')
        self.assertEqual((s2['after']['date'],s2['after']['time_label']),
                         (date(2026,11,23),'12:20–13:50'))
        self.assertNotEqual(s2['published_at'].date(),s2['after']['date'])
        substitute = self.snapshot(section='lesson',lesson_id=Lesson.objects.get(source_id='L-A-1124-1').pk)
        self.assertNotEqual(substitute['history'][0]['before']['instructor_name'],
                            substitute['history'][0]['after']['instructor_name'])
        cancelled = self.snapshot(when='2026-11-27T10:40:00+03:00')
        self.assertNotEqual(cancelled['spotlight']['lesson']['source_id'],'L-A-1127-2')
        self.assertEqual(cancelled['day']['total'],1)
        self.assertEqual(cancelled['day']['markers'][0]['marker_type'],'cancelled')

    def test_exact_start_end_moscow_midnight_monday_and_validity_end(self):
        at_start = self.snapshot(when='2026-11-23T12:20:00+03:00')
        self.assertEqual((at_start['spotlight']['kind'],at_start['spotlight']['lesson']['source_id']),('current','L-S2-23'))
        at_end = self.snapshot(when='2026-11-23T13:50:00+03:00')
        self.assertEqual(at_end['spotlight']['kind'],'next')
        self.assertNotEqual(at_end['spotlight']['lesson']['source_id'],'L-S2-23')
        midnight = self.snapshot(when='2026-11-22T21:00:00+00:00')
        self.assertEqual(midnight['today_date'],date(2026,11,23))
        sunday = self.snapshot(section='schedule',selected_date=date(2026,11,22),mode='week')
        monday = self.snapshot(section='schedule',selected_date=date(2026,11,23),mode='week')
        self.assertEqual((sunday['range_start'],sunday['range_end']),(date(2026,11,16),date(2026,11,22)))
        self.assertEqual((monday['range_start'],monday['range_end']),(date(2026,11,23),date(2026,11,29)))
        late = self.snapshot(when='2026-11-28T23:59:59+03:00')
        self.assertEqual(late['day']['state'],'finished')
        self.assertEqual(late['spotlight']['lesson']['date'],date(2026,11,30))
        last = self.snapshot(when='2026-12-06T23:59:59+03:00')
        expired = self.snapshot(when='2026-12-07T00:00:00+03:00')
        self.assertEqual(last['source']['state'],'ready')
        self.assertEqual(expired['source']['state'],'expired')
        self.assertIsNone(expired['spotlight']['lesson'])
        self.assertEqual((expired['unread'],expired['announcements'],expired['deadlines']),(0,[],[]))
        self.assertEqual(last['source']['source_as_of'],expired['source']['source_as_of'])
        self.assertEqual(last['source']['imported_at'],expired['source']['imported_at'])

    def test_partial_unknown_not_prepared_outside_and_gap_before_known_lesson(self):
        payload = json.loads((ROOT/'fixtures/s3/r7.json').read_text(encoding='utf-8'))
        initial = payload['coverage']
        for completeness in ('partial','unknown'):
            coverage = deepcopy(initial)
            own = next(x for x in coverage if x['group_id']=='G-MB21')
            own.update(completeness=completeness,active_ids=None,active_count=None)
            self.import_candidate(self.candidate(coverage=coverage))
            data = self.snapshot(when='2026-11-08T12:00:00+03:00')
            self.assertEqual(data['day']['state'],completeness)
            self.assertIsNone(data['day']['total'])
            self.assertEqual(data['day']['count_label'],'Показано 0')
            self.assertEqual(data['spotlight']['kind'],'known')
            self.assertIn('неполное',data['spotlight']['label'])
            self.assertEqual(self.snapshot(client=self.b,when='2026-11-08T12:00:00+03:00')['day']['state'],'complete_empty')
        # Пробел до полного диапазона: кандидат 9 ноября известен, гарантии "следующий" нет.
        coverage = deepcopy(initial)
        own = next(x for x in coverage if x['group_id']=='G-MB21')
        own.update(date_from='2026-11-09',active_ids=[x for x in own['active_ids'] if not x.startswith('L-A-1107-')])
        own['active_count'] = len(own['active_ids'])
        self.import_candidate(self.candidate(coverage=coverage))
        gap = self.snapshot(when='2026-11-08T12:00:00+03:00')
        self.assertEqual(gap['day']['state'],'outside')
        self.assertEqual(gap['spotlight']['kind'],'known')
        self.import_candidate(self.candidate(coverage=[x for x in initial if x['group_id']!='G-MB21']))
        unprepared = self.snapshot()
        self.assertEqual(unprepared['day']['state'],'not_prepared')
        self.assertIsNone(unprepared['day']['total'])
        self.assertIsNone(unprepared['spotlight']['lesson'])
        self.assertEqual(unprepared['source']['announcements_state'],'ready')
        self.assertTrue(unprepared['announcements'])

    def test_unknown_optional_values_and_failed_updates_do_not_invent_facts(self):
        self.import_candidate(SyntheticDatasetProvider().load(ROOT/'fixtures/s3/isolated/optional_fields.json'))
        row = self.snapshot(section='schedule',selected_date=date(2026,11,30))['days'][0]['lessons'][0]
        self.assertEqual(row['room_label'],'Аудитория не указана')
        self.assertEqual(row['instructor_name'],'Преподаватель не указан')
        original = self.snapshot(section='schedule',selected_date=date(2026,11,30))
        for outcome,message in (('invalid_update','Обновление не применено'),('source_unavailable','Источник обновления недоступен')):
            ImportAttempt.objects.create(outcome=outcome,code='isolated-test')
            data = self.snapshot(section='schedule',selected_date=date(2026,11,30))
            self.assertIn(message,data['source']['update_message'])
            self.assertEqual(data['days'],original['days'])
            self.assertEqual(data['source']['source_as_of'],original['source']['source_as_of'])

    def test_audiences_unread_and_archive_are_scoped_before_lookup(self):
        a,b = self.snapshot(section='notifications'),self.snapshot(section='notifications',client=self.b)
        def events(data):
            return {x['event_id'] for x in data['notifications']}
        self.assertEqual({x for x in events(a) if x.startswith('EV-ANN-')},
            {'EV-ANN-OPEN-PUBLISH','EV-ANN-FACULTY-PUBLISH','EV-ANN-A-DEADLINE-PUBLISH','EV-ANN-PERSONAL-A-PUBLISH'})
        self.assertEqual({x for x in events(b) if x.startswith('EV-ANN-')},
            {'EV-ANN-OPEN-PUBLISH','EV-ANN-FACULTY-PUBLISH','EV-ANN-B-DEADLINE-PUBLISH'})
        self.assertEqual(a['unread'],len(a['notifications']))
        self.assertEqual(b['unread'],len(b['notifications']))
        ordered = [(x['published_at'],x['event_id']) for x in a['notifications']]
        self.assertEqual(ordered,sorted(ordered,key=lambda x:(-x[0].timestamp(),x[1])))
        archive = self.snapshot(section='notifications',archive=True)
        archive_events = events(archive)
        self.assertIn('EV-ANN-ARCHIVE-PUBLISH',archive_events)
        self.assertIn('EV-ANN-WITHDRAW-PUBLISH',archive_events)
        self.assertNotIn('EV-ANN-FUTURE-PUBLISH',archive_events)
        withdrawn = next(x for x in archive['notifications'] if x['event_id']=='EV-ANN-WITHDRAW-PUBLISH')
        self.assertFalse(withdrawn['can_read'])
        self.assertEqual(withdrawn['body'],'')
        self.assertEqual(withdrawn['status_label'],'Объявление отозвано')
        future = Announcement.objects.get(source_id='ANN-FUTURE')
        with self.assertRaises(AccessDenied):
            self.snapshot(section='announcement',announcement_id=future.pk)
        personal = Announcement.objects.get(source_id='ANN-PERSONAL-A')
        with self.assertRaises(AccessDenied):
            self.snapshot(section='announcement',announcement_id=personal.pk,client=self.b)

    def test_announcement_publication_expiry_deadline_equality_and_read_is_not_action(self):
        future = Announcement.objects.get(source_id='ANN-FUTURE')
        with self.assertRaises(AccessDenied):
            self.snapshot(section='announcement',announcement_id=future.pk,when='2026-11-30T08:59:59.999999+03:00')
        published = self.snapshot(section='announcement',announcement_id=future.pk,when='2026-11-30T09:00:00+03:00')
        self.assertTrue(published['announcement']['available'])
        ending = self.snapshot(section='notifications',when='2026-11-10T00:00:00+03:00')
        self.assertNotIn('EV-ANN-OPEN-PUBLISH',{x['event_id'] for x in ending['notifications']})
        own = Announcement.objects.get(source_id='ANN-A-DEADLINE')
        equality = self.snapshot(section='announcement',announcement_id=own.pk,when=own.due_at.isoformat())
        elapsed = self.snapshot(section='announcement',announcement_id=own.pk,when=(own.due_at+timedelta(microseconds=1)).isoformat())
        self.assertFalse(equality['announcement']['deadline_elapsed'])
        self.assertEqual(elapsed['announcement']['deadline_label'],'Срок объявления истёк')
        notification = Notification.objects.get(announcement_change__event_id='EV-ANN-A-DEADLINE-PUBLISH')
        before = records()['announcement'][own.source_id]
        marker = self.snapshot()['marker']
        for _ in range(2):
            response = self.a.post(f'/notifications/{notification.pk}/read/',{
                'csrfmiddlewaretoken':self.a.cookies['csrftoken'].value,'context':marker})
            self.assertEqual(response.status_code,302)
        self.assertEqual(NotificationRead.objects.filter(notification=notification).count(),1)
        self.assertEqual(records()['announcement'][own.source_id],before)
        self.assertEqual(self.snapshot()['unread'],self.snapshot(client=self.b)['unread']+5)

    def test_real_routes_for_roles_queries_stale_form_and_fail_closed(self):
        own = Lesson.objects.get(source_id='L-A-1123-1')
        foreign = Lesson.objects.get(source_id='L-B-1124-1')
        announcement = Announcement.objects.get(source_id='ANN-PERSONAL-A')
        paths = ['/today/','/schedule/?date=2026-11-23&view=week',f'/lessons/{own.pk}/',f'/announcements/{announcement.pk}/','/notifications/']
        for path in paths:
            self.assertEqual(self.a.get(path).status_code,200,path)
            self.assertEqual(self.staff.get(path).status_code,403,path)
            self.assertEqual(Client().get(path).status_code,302,path)
        self.assertEqual(self.a.get(f'/lessons/{foreign.pk}/').status_code,404)
        self.assertEqual(self.b.get(f'/lessons/{own.pk}/').status_code,404)
        self.assertEqual(self.b.get(f'/announcements/{announcement.pk}/').status_code,404)
        for query in ('date=bad','date=2026-02-30','date=9999-01-01','date=2026-11-23&date=2026-11-24','view=month','group=G-EK11'):
            self.assertEqual(self.a.get('/schedule/?'+query).status_code,400,query)
            self.assertEqual(self.staff.get('/schedule/?'+query).status_code,403,query)
        for target in ('/schedule/?date=2026-11-23&view=week',f'/lessons/{own.pk}/',f'/announcements/{announcement.pk}/','/notifications/?archive=1'):
            self.assertEqual(safe_next(target,'student'),target)
            self.assertEqual(safe_next(target,'staff'),'/staff/')
        for target in ('//evil.example/','/schedule/?date=invalid','/profile/?owner=2','/schedule/?date=2026-11-23&group=x','/notifications/?archive=2'):
            self.assertEqual(safe_next(target,'student'),'/today/')
        old = self.snapshot()['marker']
        self.a.post('/logout/',{'csrfmiddlewaretoken':self.a.cookies['csrftoken'].value,'context':old})
        self.login(self.a,'student_b')
        shared = Notification.objects.get(announcement_change__event_id='EV-ANN-FACULTY-PUBLISH')
        stale = self.a.post(f'/notifications/{shared.pk}/read/',{'csrfmiddlewaretoken':self.a.cookies['csrftoken'].value,'context':old})
        self.assertEqual(stale.status_code,409)
        with patch('accounts.selectors.schedule_snapshot',side_effect=OperationalError('private diagnostic')):
            failure = self.a.get('/today/')
        self.assertEqual(failure.status_code,503)
        self.assertNotIn('Тимур',failure.content.decode())
        self.assertNotIn('private diagnostic',failure.content.decode())

    def test_rendered_private_data_is_materialized_and_get_never_marks_read(self):
        from django.template.loader import render_to_string
        sections = [('today','today.html',{}),('schedule','schedule.html',{'selected_date':date(2026,11,23),'mode':'week'}),
                    ('lesson','lesson_detail.html',{'lesson_id':Lesson.objects.get(source_id='L-A-1123-1').pk}),
                    ('announcement','announcement_detail.html',{'announcement_id':Announcement.objects.get(source_id='ANN-FACULTY').pk}),
                    ('notifications','notifications.html',{})]
        for section,template,arguments in sections:
            data = self.snapshot(section=section,**arguments)
            with self.assertNumQueries(0):
                render_to_string(template,data)
        self.assertFalse(NotificationRead.objects.exists())

    def test_retained_read_does_not_restore_revoked_group_on_new_routes(self):
        old_lesson = Lesson.objects.get(source_id='L-A-1123-1')
        old_announcement = Announcement.objects.get(source_id='ANN-A-DEADLINE')
        old_event = Notification.objects.get(announcement_change__event_id='EV-ANN-A-DEADLINE-PUBLISH')
        marker = self.snapshot()['marker']
        mark_read(self.request(),old_event.pk,marker)
        source = timezone.now().isoformat()
        before = records()['profile']['SP-A']
        after = dict(before,record_revision=before['record_revision']+1,group_id='G-S3-REVOKED',source_updated_at=source)
        payload = json.loads((ROOT/'fixtures/s3/r7.json').read_text(encoding='utf-8'))
        coverage = deepcopy(payload['coverage'])
        coverage.append(dict(entity_type='lesson',group_id='G-S3-REVOKED',period_id='P-2026-autumn',
            date_from='2026-11-07',date_until='2026-12-07',completeness='complete',active_ids=[],active_count=0))
        operations = [dict(operation='create',entity_type='group',record=dict(source_id='G-S3-REVOKED',
            record_revision=1,source_updated_at=source,name='МБ-22 (изолированный тест)',programme_id='PR-MB')),
            dict(operation='change',entity_type='profile',event_id='EV-S3-REVOKED',change_type='correction',
                expected_record_revision=before['record_revision'],before=before,after=after,reason='Изолированная смена группы')]
        self.import_candidate(self.candidate(operations=operations,coverage=coverage))
        self.assertTrue(NotificationRead.objects.filter(notification=old_event).exists())
        self.assertEqual(self.a.get(f'/lessons/{old_lesson.pk}/').status_code,404)
        self.assertEqual(self.a.get(f'/announcements/{old_announcement.pk}/').status_code,404)
        self.assertEqual(self.a.get(f'/notifications/{old_event.pk}/').status_code,404)
        today = self.snapshot(when='2026-11-23T12:20:00+03:00')
        self.assertEqual((today['day']['total'],today['day']['state']),(0,'complete_empty'))
        self.assertIsNone(today['spotlight']['lesson'])
        with self.assertRaises(AccessDenied) as stale:
            mark_read(self.request(),old_event.pk,marker)
        self.assertEqual(stale.exception.code,'stale_context')
        with self.assertRaises(AccessDenied) as missing:
            mark_read(self.request(),old_event.pk,today['marker'])
        self.assertEqual(missing.exception.code,'not_found')

    def test_draft_publish_correction_archive_and_old_read_are_distinct(self):
        now = timezone.now()
        record = dict(source_id='ANN-TEST-DRAFT',record_revision=1,source_updated_at=now.isoformat(),
            title='Непубличный тестовый черновик',body='Секрет черновика в изолированном тесте.',
            audience='all_students',faculty_id=None,group_id=None,student_id=None,
            published_at=(now-timedelta(minutes=1)).isoformat(),visible_until='2026-12-07T00:00:00+03:00',
            due_at=None,status='draft')
        self.import_candidate(self.candidate(operations=[dict(operation='create',entity_type='announcement',
            event_id='EV-S3-DRAFT',record=record)]))
        announcement = Announcement.objects.get(source_id='ANN-TEST-DRAFT')
        for archive in (False,True):
            data = self.snapshot(section='notifications',archive=archive)
            self.assertNotIn('EV-S3-DRAFT',{x['event_id'] for x in data['notifications']})
        with self.assertRaises(AccessDenied):
            self.snapshot(section='announcement',announcement_id=announcement.pk)
        def change(change_type,event,**fields):
            before = records()['announcement']['ANN-TEST-DRAFT']
            after = dict(before,record_revision=before['record_revision']+1,source_updated_at=timezone.now().isoformat(),**fields)
            self.import_candidate(self.candidate(operations=[dict(operation='change',entity_type='announcement',
                event_id=event,expected_record_revision=before['record_revision'],change_type=change_type,
                before=before,after=after,reason='Изолированная проверка публикации')]))
        change('publish','EV-S3-PUBLISH',status='published',title='Опубликованное тестовое объявление',body='Опубликованный текст.')
        event = Notification.objects.get(announcement_change__event_id='EV-S3-PUBLISH')
        mark_read(self.request(),event.pk,self.snapshot()['marker'])
        after_read = self.snapshot()['unread']
        change('correction','EV-S3-CORRECTION',body='Уточнённый опубликованный текст.')
        active = self.snapshot(section='notifications')
        archive = self.snapshot(section='notifications',archive=True)
        self.assertIn('EV-S3-CORRECTION',{x['event_id'] for x in active['notifications']})
        self.assertNotIn('EV-S3-PUBLISH',{x['event_id'] for x in active['notifications']})
        self.assertNotIn('EV-S3-DRAFT',{x['event_id'] for x in archive['notifications']})
        old = next(x for x in archive['notifications'] if x['event_id']=='EV-S3-PUBLISH')
        self.assertTrue(old['is_historical'])
        self.assertTrue(old['is_read'])
        self.assertFalse(old['can_read'])
        self.assertEqual(old['body'],'Опубликованный текст.')
        self.assertEqual(old['current']['body'],'Уточнённый опубликованный текст.')
        self.assertEqual(active['unread'],after_read+1)
        self.assertTrue(NotificationRead.objects.filter(notification=event).exists())

    def withdraw(self):
        before = records()['announcement']['ANN-FACULTY']
        after = dict(before,record_revision=before['record_revision']+1,
                     source_updated_at=timezone.now().isoformat(),status='withdrawn')
        return self.candidate(operations=[dict(operation='change',entity_type='announcement',
            event_id='EV-TEST-WITHDRAW',expected_record_revision=before['record_revision'],change_type='withdraw',
            before=before,after=after,reason='Изолированная проверка гонки')])

    def withdrawal_race(self,first):
        marker = self.snapshot()['marker']
        request = self.request()
        target = Notification.objects.get(announcement_change__event_id='EV-ANN-FACULTY-PUBLISH').pk
        batch = self.withdraw()
        held,release = threading.Event(),threading.Event()
        pids,results = {},{}
        def worker(name):
            def barrier(execute,sql,params,many,context):
                result = execute(sql,params,many,context)
                if name == first and 'accounts_authorizationguard' in sql and 'FOR UPDATE' in sql:
                    held.set()
                    if not release.wait(10):
                        raise RuntimeError('guard barrier timeout')
                return result
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids[name] = cursor.fetchone()[0]
                with connections['default'].execute_wrapper(barrier):
                    if name == 'read':
                        mark_read(request,target,marker)
                    else:
                        self.import_candidate(batch)
                results[name] = 'ok'
            except AccessDenied as exc:
                results[name] = exc.code
            except Exception as exc:
                results[name] = type(exc).__name__+':'+str(exc)
            finally:
                connections['default'].close()
        second = 'withdraw' if first == 'read' else 'read'
        a,b = threading.Thread(target=worker,args=(first,)),threading.Thread(target=worker,args=(second,))
        a.start()
        try:
            self.assertTrue(held.wait(5))
            b.start()
            deadline = time.monotonic()+5
            blocked = False
            while time.monotonic() < deadline:
                if second in pids:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_blocking_pids(%s)',[pids[second]])
                        if pids[first] in cursor.fetchone()[0]:
                            blocked = True
                            break
                time.sleep(.01)
            self.assertTrue(blocked,'Реально наблюдаемое ожидание PostgreSQL lock обязательно')
        finally:
            release.set()
            a.join(10)
            if b.ident:
                b.join(10)
        self.assertNotEqual(pids['read'],pids['withdraw'])
        self.assertEqual(results,{'read':'ok' if first=='read' else 'stale_context','withdraw':'ok'})
        self.assertEqual(NotificationRead.objects.filter(notification_id=target).count(),1 if first=='read' else 0)
        self.assertEqual(Announcement.objects.get(source_id='ANN-FACULTY').status,'withdrawn')
        self.assertNotIn('EV-ANN-FACULTY-PUBLISH',{x['event_id'] for x in self.snapshot(section='notifications')['notifications']})

    def test_read_then_withdrawal_use_real_connection_order(self):
        self.withdrawal_race('read')

    def test_withdrawal_then_read_rejects_old_context(self):
        self.withdrawal_race('withdraw')

    def test_expiry_crosses_during_target_lock_and_is_rechecked(self):
        now = timezone.now()
        until = now + timedelta(minutes=1)
        announcement = dict(source_id='ANN-LOCK-EXPIRY',record_revision=1,source_updated_at=now.isoformat(),
            title='Изолированное короткое объявление',body='Только тест.',audience='all_students',faculty_id=None,
            group_id=None,student_id=None,published_at=(now-timedelta(minutes=1)).isoformat(),
            visible_until=until.isoformat(),due_at=None,status='published')
        self.import_candidate(self.candidate(operations=[dict(operation='create',entity_type='announcement',
            event_id='EV-LOCK-EXPIRY',record=announcement)]))
        marker = self.snapshot()['marker']
        target = Notification.objects.get(announcement_change__event_id='EV-LOCK-EXPIRY')
        request = self.request()
        with patch('accounts.access.timezone.now',side_effect=[until-timedelta(microseconds=1),until]), self.assertRaises(AccessDenied) as error:
            mark_read(request,target.pk,marker)
        self.assertEqual(error.exception.code,'not_found')
        self.assertFalse(NotificationRead.objects.filter(notification=target).exists())
