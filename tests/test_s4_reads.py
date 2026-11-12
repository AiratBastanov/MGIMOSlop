"""S4: независимые численные ожидания, scope, время и реальные гонки PG.

Предметные часы переопределяются только в изолированной test DB. Обычный сервер
и двухчасовая security expiry используют настоящее время.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, connections
from django.test import Client, RequestFactory, TransactionTestCase, SimpleTestCase
from django.utils import timezone

from accounts.access import AccessDenied
from accounts.models import SessionContext
from accounts.provisioning import provision
from accounts.selectors import private_snapshot
from accounts.views import safe_next
from data_import.contracts import content_hash, validate
from data_import.models import Dataset, ImportAttempt
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply, initialize
from data_import.state import records
from notifications.actions import mark_read
from notifications.models import Notification, NotificationRead
from studies.models import Enrollment, FinalResult, OutstandingRequirement, AssessmentEvent, Lesson
from studies.selectors import exact_moscow, clock_label
from tests.s4_fixtures import build_payload, change, create

ROOT = Path(__file__).resolve().parents[1]


class S4ExactDisplayTests(SimpleTestCase):
    def test_source_seconds_and_fraction_are_not_rounded_in_visible_time(self):
        self.assertEqual(exact_moscow(datetime.fromisoformat('2026-11-10T15:00:59.125000+00:00')),
                         '10.11.2026, 18:00:59.125 МСК')
        self.assertEqual(clock_label(datetime.fromisoformat('2026-11-10T18:00:00+03:00')), '18:00')


class S4ReadTests(TransactionTestCase):
    def setUp(self):
        initialize()
        for revision in range(1, 5):
            folder = 's1' if revision < 3 else 's2'
            apply(SyntheticDatasetProvider().load(ROOT / f'fixtures/{folder}/r{revision}.json'),
                  expected_revision=revision - 1, operator='isolated-s4-reads')
        self.payload = build_payload(timezone.now().isoformat())
        self.accept(self.payload)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / 'credentials.json'
        provision(path)
        self.credentials = json.loads(path.read_text(encoding='utf-8'))
        self.a, self.b, self.staff = (Client(enforce_csrf_checks=True) for _ in range(3))
        for client, name in ((self.a, 'student_a'), (self.b, 'student_b'), (self.staff, 'staff_demo')):
            client.get('/login/')
            response = client.post('/login/', {'username': name, 'password': self.credentials[name],
                'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
            self.assertEqual(response.status_code, 302)

    def accept(self, payload):
        payload['content_sha256'] = content_hash(payload)
        batch = validate(payload)
        result = apply(batch, expected_revision=batch.parent, operator='isolated-s4-reads')
        self.payload = deepcopy(payload)
        return result

    def candidate(self, operations=(), *, coverage=None, **changes):
        payload = deepcopy(self.payload)
        revision = Dataset.objects.get().current_revision
        payload.update(dataset_revision=revision + 1, parent_revision=revision,
                       source_as_of=timezone.now().isoformat(), operations=list(operations), **changes)
        if coverage is not None:
            payload['academic_coverage'] = coverage
        payload['content_sha256'] = content_hash(payload)
        return payload

    def request(self, client=None):
        request = RequestFactory().get('/study/')
        request.session = SessionStore(session_key=(client or self.a).cookies['sessionid'].value)
        dict(request.session.items())
        return request

    def snapshot(self, *, section='study', client=None, when='2026-11-10T12:00:00+03:00', **kwargs):
        with patch('accounts.selectors.read_now', return_value=datetime.fromisoformat(when)):
            return private_snapshot(self.request(client), section=section, **kwargs)

    def summary(self, client=None):
        value = self.snapshot(client=client)['summary']
        return value['X'], value['Y'], value['Z'], value['N']

    def source(self):
        return timezone.now().isoformat()

    def add_requirement(self, source_id='Q-EXTRA', enrollment='E-A-4', due=None):
        source = self.source()
        return {'operation': 'create', 'entity_type': 'requirement', 'event_id': 'EV-' + source_id,
            'record': dict(source_id=source_id, record_revision=1, source_updated_at=source,
                enrollment_id=enrollment, result_id=None, reason='Не представлен отдельный итоговый материал',
                status='open', opened_at=source, due_at=due, closed_at=None)}

    def coverage_with(self, *, student='SP-A', entity='requirement', add=(), remove=()):
        coverage = deepcopy(self.payload['academic_coverage'])
        row = next(x for x in coverage if x['student_id'] == student and x['entity_type'] == entity)
        row['record_ids'] = [x for x in row['record_ids'] if x not in remove] + list(add)
        row['record_count'] = len(row['record_ids'])
        return coverage

    def test_independent_counts_missing_result_and_positive_with_multiple_requirements(self):
        self.assertEqual(self.summary(), (3, 1, 1, 5))
        self.assertEqual(self.summary(self.b), (2, 0, 2, 4))
        a = self.snapshot()
        missing = next(x for x in a['courses'] if x['source_id'] == 'E-A-5')
        self.assertEqual(missing['result_label'], 'Итог не внесён')
        self.assertIsNone(missing['result'])
        self.accept(self.candidate([self.add_requirement()], coverage=self.coverage_with(add=['Q-EXTRA'])))
        self.assertEqual(self.summary(), (3, 1, 1, 5))
        source = self.source()
        current = records(schema_version=4)
        self.accept(self.candidate([
            change('final_result', current['final_result']['R-A-4'], source, 'EV-CORRECT', grade=3),
            change('requirement', current['requirement']['Q-A-4'], source, 'EV-CLOSE',
                   change_type='close', status='closed', closed_at=source),
        ]))
        self.assertEqual(self.summary(), (3, 1, 1, 5))
        row = next(x for x in self.snapshot()['courses'] if x['source_id'] == 'E-A-4')
        self.assertEqual(row['result_label'], '3')
        self.assertEqual([x['source_id'] for x in row['open_requirements']], ['Q-EXTRA'])
        self.assertEqual(row['open_requirements'][0]['due_label'], 'Срок не указан')
        self.assertFalse(row['open_requirements'][0]['overdue'])
        source = self.source()
        self.accept(self.candidate([change('requirement', records(schema_version=4)['requirement']['Q-EXTRA'],
            source, 'EV-CLOSE-EXTRA', change_type='close', status='closed', closed_at=source)]))
        self.assertEqual(self.summary(), (4, 0, 1, 5))

    def test_each_incomplete_domain_disables_exact_summary_and_missing_is_not_failure(self):
        complete = deepcopy(self.payload['academic_coverage'])
        for entity in ('enrollment', 'final_result', 'requirement'):
            with self.subTest(entity=entity):
                coverage = deepcopy(complete)
                row = next(x for x in coverage if x['student_id'] == 'SP-A' and x['entity_type'] == entity)
                row.update(completeness='partial', record_ids=None, record_count=None)
                self.accept(self.candidate(coverage=coverage))
                data = self.snapshot()
                self.assertFalse(data['summary']['complete'])
                self.assertEqual(tuple(data['summary'][x] for x in ('X', 'Y', 'Z', 'N')), (None, None, None, None))
                self.assertIn('неполные', data['summary']['message'])
                self.assertEqual(self.summary(self.b), (2, 0, 2, 4))
                missing = next(x for x in data['courses'] if x['source_id'] == 'E-A-5')
                self.assertIsNone(missing['result'])
                if entity == 'final_result':
                    self.assertIn('источник неполон', missing['result_label'])
                today = self.snapshot(section='today')
                self.assertIn('неполные', today['academic_warning'])

    def test_expired_and_before_validity_keep_as_of_facts_without_current_claims(self):
        for moment in ('2026-12-07T00:00:00+03:00', '2026-11-01T00:00:00+03:00'):
            with self.subTest(moment=moment):
                data = self.snapshot(when=moment)
                self.assertFalse(data['summary']['complete'])
                self.assertIsNone(data['summary']['N'])
                self.assertIsNone(data['next_assessment'])
                self.assertEqual(len(data['courses']), 5)
                self.assertEqual(data['unread'], 0)
                today = self.snapshot(section='today', when=moment)
                self.assertTrue(today['academic_warning'])
                self.assertFalse(today['attention_open_count'])
        source = self.snapshot()['source']
        ImportAttempt.objects.create(outcome='invalid_update', code='isolated-s4')
        failed = self.snapshot()
        self.assertIn('Обновление не применено', failed['source']['update_message'])
        self.assertEqual(failed['source']['source_as_of'], source['source_as_of'])
        self.assertEqual(self.summary(), (3, 1, 1, 5))

    def withdraw(self, source_id='E-A-1'):
        row = records(schema_version=4)['enrollment'][source_id]
        operations = [change('enrollment', row, self.source(), 'EV-WITHDRAW-' + source_id, status='withdrawn')]
        coverage = deepcopy(self.payload['academic_coverage'])
        # Retained facts remain stored, but complete academic scope excludes the
        # withdrawn Enrollment and its dependent data/assessment eligibility.
        for spec in coverage:
            if spec['student_id'] != row['student_id']:
                continue
            if spec['entity_type'] == 'enrollment':
                ids = [x for x in spec['record_ids'] if x != source_id]
            elif spec['entity_type'] in ('final_result', 'requirement'):
                current = records(schema_version=4)[spec['entity_type']]
                ids = [x for x in spec['record_ids'] if current[x]['enrollment_id'] != source_id]
            else:
                current = records(schema_version=4)['assessment']
                ids = [x for x in spec['record_ids'] if current[x]['course_id'] != row['course_id']]
            spec.update(record_ids=ids, record_count=len(ids))
        return self.candidate(operations, coverage=coverage)

    def test_complete_zero_enrollment_and_revoked_scope_retains_facts(self):
        for source_id in ('E-B-1', 'E-B-2', 'E-B-3', 'E-B-4'):
            self.accept(self.withdraw(source_id))
        self.assertEqual(self.summary(self.b), (0, 0, 0, 0))
        data = self.snapshot(client=self.b)
        self.assertEqual(data['courses'], [])
        self.assertTrue(data['summary']['complete'])
        self.assertIsNone(data['next_assessment'])
        self.assertTrue(FinalResult.objects.filter(enrollment__source_id='E-B-1').exists())

    def test_scope_foreign_ids_period_filter_and_read_do_not_modify_requirements(self):
        enrollment = Enrollment.objects.get(source_id='E-A-1')
        assessment = AssessmentEvent.objects.get(source_id='AS-A-1')
        event = Notification.objects.get(final_result_change__result__source_id='R-A-1')
        paths = [f'/study/enrollments/{enrollment.pk}/', f'/assessments/{assessment.pk}/', f'/notifications/{event.pk}/']
        for path in paths:
            self.assertEqual(self.b.get(path).status_code, 404, path)
            self.assertEqual(self.staff.get(path).status_code, 403, path)
            self.assertEqual(Client().get(path).status_code, 302, path)
        self.assertEqual(self.a.get('/study/?period=P-FOREIGN').status_code, 404)
        for query in ('view=grades', 'filter=all&filter=open', 'student=SP-B', 'view=assessments&filter=open'):
            self.assertEqual(self.a.get('/study/?' + query).status_code, 400)
        self.assertEqual(self.a.get('/study/?filter=open').context['filtered_count'], 1)
        before = list(OutstandingRequirement.objects.order_by('pk').values())
        count = NotificationRead.objects.count()
        self.a.get(paths[0]); self.a.get(paths[1]); self.a.get(paths[2])
        self.assertEqual(NotificationRead.objects.count(), count)
        marker = self.snapshot()['marker']
        response = self.a.post(f'/notifications/{event.pk}/read/', {'context': marker,
            'csrfmiddlewaretoken': self.a.cookies['csrftoken'].value})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(list(OutstandingRequirement.objects.order_by('pk').values()), before)
        self.accept(self.withdraw())
        self.assertEqual(self.a.get(paths[0]).status_code, 404)
        self.assertEqual(self.a.get(paths[1]).status_code, 404)
        self.assertEqual(self.a.get(paths[2]).status_code, 404)
        self.assertTrue(NotificationRead.objects.filter(notification=event).exists())
        self.assertEqual(safe_next('/study/?view=assessments', 'student'), '/study/?view=assessments')
        self.assertEqual(safe_next('/study/?student=SP-B', 'student'), '/today/')

    def test_result_history_notifications_share_current_and_historical_facts(self):
        source = self.source()
        current = records(schema_version=4)
        self.accept(self.candidate([
            change('final_result', current['final_result']['R-A-4'], source, 'EV-CORRECT-HISTORY', grade=3),
            change('requirement', current['requirement']['Q-A-4'], source, 'EV-CLOSE-HISTORY',
                   change_type='close', status='closed', closed_at=source)]))
        self.assertEqual(self.summary(), (4, 0, 1, 5))
        enrollment = Enrollment.objects.get(source_id='E-A-4')
        detail = self.snapshot(section='enrollment', enrollment_id=enrollment.pk)
        self.assertEqual(detail['enrollment']['result_label'], '3')
        corrected = next(x for x in detail['history'] if x['event_id'] == 'EV-CORRECT-HISTORY')
        self.assertEqual((corrected['before']['label'], corrected['after']['label']), ('2', '3'))
        feed = self.snapshot(section='notifications', archive=True)
        event = next(x for x in feed['notifications'] if x['event_id'] == 'EV-CORRECT-HISTORY')
        self.assertEqual(event['current'], detail['enrollment'])
        old = next(x for x in feed['notifications'] if x['kind'] == 'result' and x['after']['source_id'] == 'R-A-4' and x['is_historical'])
        self.assertEqual(old['after']['label'], '2')
        self.assertEqual(old['current']['result_label'], '3')
        self.assertFalse(old['can_read'])
        self.assertNotIn(old['id'], [x['id'] for x in self.snapshot(section='notifications')['notifications']])

    def test_assessment_move_cancel_optional_fields_dates_and_no_inferred_obligation(self):
        before_counts = FinalResult.objects.count(), OutstandingRequirement.objects.count(), Lesson.objects.count()
        current = records(schema_version=4)['assessment']['AS-A-1']
        source = self.source()
        self.accept(self.candidate([change('assessment', current, source, 'EV-AS-MOVE', change_type='move',
            starts_at='2026-11-12T23:00:00+03:00', ends_at='2026-11-12T23:30:00+03:00',
            room=None, location_status='not_provided', instructor_id=None, instructor_status='not_provided')]))
        data = self.snapshot(study_mode='assessments')
        event = data['next_assessment']
        self.assertEqual((event['date'].isoformat(), event['time_label']), ('2026-11-12', '23:00–23:30'))
        self.assertEqual(event['room_label'], 'Аудитория не указана')
        self.assertEqual(event['instructor_name'], 'Преподаватель не указан')
        self.assertEqual(event['change']['title'], 'Событие сессии перенесено')
        finished = self.snapshot(study_mode='assessments', when='2026-11-12T20:30:00+00:00')
        self.assertEqual(len(finished['past_assessments']), 1)
        self.assertIsNone(finished['next_assessment'])
        self.assertEqual((FinalResult.objects.count(), OutstandingRequirement.objects.count(), Lesson.objects.count()), before_counts)
        self.accept(self.candidate([change('assessment', records(schema_version=4)['assessment']['AS-A-1'],
            self.source(), 'EV-AS-CANCEL', change_type='cancel', status='cancelled')]))
        cancelled = self.snapshot(study_mode='assessments')
        self.assertEqual(cancelled['upcoming_assessments'][0]['status'], 'cancelled')
        self.assertIsNone(cancelled['next_assessment'])

    def test_exact_due_equality_moscow_boundary_and_incomplete_assessment(self):
        due = OutstandingRequirement.objects.get(source_id='Q-A-4').due_at
        at = self.snapshot(when=due.isoformat())
        req = next(x for x in at['courses'] if x['source_id'] == 'E-A-4')['open_requirements'][0]
        self.assertFalse(req['overdue'])
        after = self.snapshot(when=(due + timedelta(microseconds=1)).isoformat())
        self.assertTrue(next(x for x in after['courses'] if x['source_id'] == 'E-A-4')['open_requirements'][0]['overdue'])
        midnight = self.snapshot(when='2026-11-09T21:00:00+00:00')
        self.assertEqual(midnight['today_date'].isoformat(), '2026-11-10')
        coverage = deepcopy(self.payload['academic_coverage'])
        row = next(x for x in coverage if x['student_id'] == 'SP-A' and x['entity_type'] == 'assessment')
        row.update(completeness='unknown', record_ids=None, record_count=None)
        self.accept(self.candidate(coverage=coverage))
        data = self.snapshot(study_mode='assessments')
        self.assertFalse(data['assessment_complete'])
        self.assertIn('известное', data['next_assessment_label'])
        self.assertTrue(data['summary']['complete'])

    def test_today_priorities_overflow_and_shared_requirement_projection(self):
        source = self.source()
        additions = [self.add_requirement('Q-OVER-' + str(i), enrollment='E-A-1', due='2026-11-09T18:00:00+03:00')
                     for i in range(4)]
        self.accept(self.candidate(additions, coverage=self.coverage_with(add=[x['record']['source_id'] for x in additions])))
        today = self.snapshot(section='today')
        self.assertEqual(today['attention_overdue_count'], 4)
        self.assertEqual(today['attention_open_count'], 5)
        self.assertEqual(len(today['attention']), 3)
        self.assertGreaterEqual(today['attention_overflow'], 2)
        self.assertTrue(all(x['overdue'] for x in today['attention']))
        route = next(x for x in today['attention_links'] if x['overdue_count'] == 4)
        self.assertIn('filter=open', route['url'])
        self.assertEqual(route['count'], 5)
        read_before = NotificationRead.objects.count()
        self.a.get('/today/'); self.a.get(route['url'])
        self.assertEqual(NotificationRead.objects.count(), read_before)
        # The retained S2 schedule covers only 23 November. Academic completeness
        # must not turn its unknown 10 November schedule into a confirmed zero.
        self.assertIsNone(today['day']['total'])

    def test_near_deadline_precedes_distant_change_independently_of_read(self):
        source = self.source()
        announcement = create('announcement', source, 'ANN-NEAR', title='Близкий срок объявления',
            body='Информационное объявление, не учебная обязанность.', audience='student',
            faculty_id=None, group_id=None, student_id='SP-A', published_at=source,
            visible_until=None, due_at='2026-11-09T10:00:00+03:00', status='published')
        self.accept(self.candidate([announcement]))
        today = self.snapshot(section='today', when='2026-11-08T12:00:00+03:00')
        self.assertEqual(today['attention'][0]['kind'], 'announcement')
        self.assertEqual(today['attention'][0]['title'], 'Близкий срок объявления')
        self.assertEqual(today['attention'][-1]['kind'], 'schedule')
        self.assertIn('23.11.2026', today['attention'][-1]['detail'])
        event = Notification.objects.get(announcement_change__announcement__source_id='ANN-NEAR')
        mark_read(self.request(), event.pk, today['marker'])
        after = self.snapshot(section='today', when='2026-11-08T12:00:00+03:00')
        self.assertEqual(today['attention'], after['attention'])
        self.assertEqual(after['unread'], today['unread'] - 1)

    def test_same_group_without_matching_enrollment_cannot_open_assessment(self):
        source = self.source()
        legacy_course = next(iter(records(schema_version=4)['lesson'].values()))['course_id']
        event = create('assessment', source, 'AS-NO-ENROLLMENT', group_id='G-MB21',
            course_id=legacy_course, period_id='P-2026-autumn', assessment_kind='exam',
            instructor_id=None, instructor_status='not_provided',
            starts_at='2026-11-11T09:00:00+03:00', ends_at='2026-11-11T10:30:00+03:00',
            room=None, location_status='not_provided', status='active')
        self.accept(self.candidate([event]))
        target = AssessmentEvent.objects.get(source_id='AS-NO-ENROLLMENT')
        notification = Notification.objects.get(assessment_change__assessment=target)
        self.assertEqual(self.a.get(f'/assessments/{target.pk}/').status_code, 404)
        self.assertEqual(self.a.get(f'/notifications/{notification.pk}/').status_code, 404)
        self.assertNotIn(target.pk, [x['id'] for x in self.snapshot()['past_assessments'] + self.snapshot()['upcoming_assessments']])

    def race(self, first):
        marker = self.snapshot()['marker']
        request = self.request()
        event = Notification.objects.get(final_result_change__result__source_id='R-A-1')
        payload = self.withdraw()
        batch = validate(payload)
        held, release = threading.Event(), threading.Event()
        pids, results = {}, {}
        def worker(name):
            def barrier(execute, sql, params, many, context):
                value = execute(sql, params, many, context)
                if name == first and 'accounts_authorizationguard' in sql and 'FOR UPDATE' in sql:
                    held.set()
                    if not release.wait(10):
                        raise RuntimeError('guard barrier timeout')
                return value
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids[name] = cursor.fetchone()[0]
                with connections['default'].execute_wrapper(barrier):
                    if name == 'read':
                        mark_read(request, event.pk, marker)
                    else:
                        apply(batch, expected_revision=batch.parent, operator='isolated-s4-race')
                results[name] = 'ok'
            except AccessDenied as exc:
                results[name] = exc.code
            except Exception as exc:
                results[name] = type(exc).__name__ + ':' + str(exc)
            finally:
                connections['default'].close()
        second = 'withdraw' if first == 'read' else 'read'
        a, b = threading.Thread(target=worker, args=(first,)), threading.Thread(target=worker, args=(second,))
        a.start()
        try:
            self.assertTrue(held.wait(5))
            b.start()
            deadline, blocked = time.monotonic() + 5, False
            while time.monotonic() < deadline:
                if second in pids:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_blocking_pids(%s)', [pids[second]])
                        if pids[first] in cursor.fetchone()[0]:
                            blocked = True
                            break
                time.sleep(.01)
            self.assertTrue(blocked, 'Нужно наблюдаемое ожидание PostgreSQL lock')
        finally:
            release.set()
            a.join(10)
            if b.ident:
                b.join(10)
        self.assertNotEqual(pids['read'], pids['withdraw'])
        self.assertEqual(results, {'read': 'ok' if first == 'read' else 'stale_context', 'withdraw': 'ok'})
        self.assertEqual(NotificationRead.objects.filter(notification=event).count(), 1 if first == 'read' else 0)
        self.assertEqual(self.a.get(f'/notifications/{event.pk}/').status_code, 404)

    def test_read_then_enrollment_withdrawal_on_real_connections(self):
        self.race('read')

    def test_enrollment_withdrawal_then_read_on_real_connections(self):
        self.race('withdraw')

    def test_academic_snapshot_is_discarded_after_concurrent_scope_change(self):
        request = self.request()
        batch = validate(self.withdraw())
        held, release = threading.Event(), threading.Event()
        result = {}
        def reader():
            def barrier(execute, sql, params, many, context):
                value = execute(sql, params, many, context)
                if 'FROM "studies_finalresult"' in sql:
                    held.set()
                    if not release.wait(10):
                        raise RuntimeError('snapshot barrier timeout')
                return value
            try:
                with connections['default'].execute_wrapper(barrier):
                    result['data'] = private_snapshot(request, section='study')
            except AccessDenied as exc:
                result['denied'] = exc.code
            finally:
                connections['default'].close()
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            self.assertTrue(held.wait(5))
            apply(batch, expected_revision=batch.parent, operator='isolated-s4-snapshot-race')
        finally:
            release.set()
            thread.join(10)
        self.assertEqual(result, {'denied': 'stale_context'})

    def test_new_target_rechecks_source_and_absolute_session_expiry_after_lock(self):
        event = Notification.objects.get(final_result_change__result__source_id='R-A-1')
        marker = self.snapshot()['marker']
        request = self.request()
        # Only source/security clocks of this isolated writer call move; data are
        # confined to this test DB. A short source must still contain all retained
        # occurrence intervals: move them explicitly rather than bypass validation.
        anchor = timezone.now()
        source_until = anchor + timedelta(minutes=2)
        current = records(schema_version=4)
        moves = []
        index = 0
        for kind in ('lesson', 'assessment'):
            for row in current[kind].values():
                index += 1
                moves.append(change(kind, row, anchor.isoformat(), 'EV-SHORT-' + row['source_id'],
                    change_type='move', starts_at=(anchor + timedelta(seconds=10 * index)).isoformat(),
                    ends_at=(anchor + timedelta(seconds=10 * index + 5)).isoformat()))
        short = self.candidate(moves, valid_until=source_until.isoformat())
        short['coverage'] = []
        self.accept(short)
        marker = self.snapshot()['marker']
        with patch('accounts.access.timezone.now', side_effect=[source_until - timedelta(microseconds=1), source_until]), self.assertRaises(AccessDenied) as error:
            mark_read(request, event.pk, marker)
        self.assertEqual(error.exception.code, 'not_found')
        self.assertFalse(NotificationRead.objects.filter(notification=event).exists())
        # Restore source validity via another accepted version, then cross the
        # immutable two-hour session deadline while acquiring the target lock.
        self.accept(self.candidate(valid_until='2026-12-07T00:00:00+03:00'))
        marker = self.snapshot()['marker']
        context = SessionContext.objects.get(pk=request.session['context_id'])
        with patch('accounts.access.timezone.now', side_effect=[context.expires_at - timedelta(microseconds=1), context.expires_at]), self.assertRaises(AccessDenied) as error:
            mark_read(request, event.pk, marker)
        self.assertEqual(error.exception.code, 'session_expired')
        self.assertFalse(NotificationRead.objects.filter(notification=event).exists())
