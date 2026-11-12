"""S5: сохранённый workflow, точная идемпотентность, PostgreSQL инварианты."""
from copy import deepcopy
import threading
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction, IntegrityError
from django.test import Client, TransactionTestCase
from django.utils import timezone

from accounts.access import AccessDenied, current_actor, logout_session
from accounts.models import Identity, SessionContext, StaffScope
from data_import.contracts import validate
from data_import.services import apply, get_head, check_integrity
from data_import.provider import SyntheticDatasetProvider
from data_import.state import canonical_state
from notifications.models import Notification
from student_requests.models import Request, RequestEvent
from student_requests.selectors import request_page
from student_requests.services import (RequestError, bootstrap_examples, issue_submission,
    submit_request, transition_request)
from student_requests.workflow import REQUEST_TYPE, TRANSITIONS, available_actions
from tests import test_auth
from tests.test_import import signed

ROOT = test_auth.ROOT


class RequestDomainTests(TransactionTestCase):
    # Общий S2 setup без наследования его test_*: прежние тесты не дублируются.
    setUp = test_auth.AuthTests.setUp
    login = test_auth.AuthTests.login
    request = test_auth.AuthTests.request
    page = test_auth.AuthTests.page
    post = test_auth.AuthTests.post
    marker = test_auth.AuthTests.marker
    scope_change = test_auth.AuthTests.scope_change

    def actors(self):
        self.login()
        self.staff = Client(enforce_csrf_checks=True)
        self.login(self.staff, 'staff_demo')
        return current_actor(self.request())

    def create(self, comment='Для демонстрации', token=None, client=None, request_type=REQUEST_TYPE):
        req = self.request(client)
        actor = current_actor(req)
        return submit_request(req, actor.marker, token or issue_submission(actor), request_type, comment)

    def change(self, row, action, explanation='', version=None, client=None):
        req = self.request(client or self.staff)
        actor = current_actor(req)
        return transition_request(req, row.pk, actor.marker, action,
                                  row.version if version is None else version, explanation)

    def state(self):
        return (list(Request.objects.order_by('pk').values()),
                list(RequestEvent.objects.order_by('pk').values()),
                list(Notification.objects.filter(request_event__isnull=False).order_by('pk').values()))

    def counts(self):
        return (Request.objects.count(), RequestEvent.objects.count(),
                Notification.objects.filter(request_event__isnull=False).count())

    def at_state(self, status):
        row = Request.objects.get(pk=self.create()['id'])
        if status != 'submitted':
            self.change(row, 'take')
            row.refresh_from_db()
        if status in ('ready', 'rejected'):
            self.change(row, 'complete' if status == 'ready' else 'reject', 'Обоснованный результат (демо)')
            row.refresh_from_db()
        return row

    def test_every_transition_and_terminal_state_uses_canonical_actions(self):
        self.actors()
        for status in ('submitted', 'in_progress', 'ready', 'rejected'):
            expected_actions = {key for key, rule in TRANSITIONS.items() if status in rule.from_states}
            self.assertEqual({action['action'] for action in available_actions(status)}, expected_actions)
            for action in ('take', 'complete', 'reject', 'submit', 'reopen', 'delete'):
                with self.subTest(status=status, action=action):
                    row = self.at_state(status)
                    before = self.state()
                    if action in expected_actions:
                        self.change(row, action, '' if action == 'take' else 'Разрешённое решение (демо)')
                        row.refresh_from_db()
                        self.assertEqual(row.status, TRANSITIONS[action].to_status)
                        self.assertEqual(row.events.count(), row.version)
                        event = row.events.get(version=row.version)
                        self.assertEqual(event.from_status, status)
                        self.assertEqual(event.to_status, row.status)
                        self.assertEqual(event.actor.username, 'staff_demo')
                        self.assertEqual(event.notification.published_at, event.recorded_at)
                    else:
                        with self.assertRaises(RequestError) as caught:
                            self.change(row, action, 'Нельзя')
                        self.assertEqual(caught.exception.status, 409)
                        self.assertEqual(self.state(), before)

    def test_normalization_length_boundary_unknown_type_and_correction(self):
        actor = self.actors()
        token = issue_submission(actor)
        for comment, request_type in [('я' * 501, REQUEST_TYPE), ('ok', 'unknown'), ('a\x00b', REQUEST_TYPE)]:
            with self.subTest(comment=comment[:10], request_type=request_type):
                with self.assertRaises(RequestError):
                    self.create(comment, token=token, request_type=request_type)
                self.assertEqual(self.counts(), (0, 0, 0))
        accepted = self.create(' \r\n' + 'я' * 500 + '\r\n ', token=token)
        row = Request.objects.get(pk=accepted['id'])
        self.assertEqual(row.comment, 'я' * 500)
        self.assertEqual(self.counts(), (1, 1, 1))
        composed = self.create('  е\u0308\r\nтекст\rещё  ')
        self.assertEqual(Request.objects.get(pk=composed['id']).comment, 'ё\nтекст\nещё')
        blank = self.create(' \r\n\t\u00a0 ')
        self.assertEqual(Request.objects.get(pk=blank['id']).comment, '')

    def test_required_explanations_and_completed_length_boundary(self):
        self.actors()
        for status, action in [('submitted', 'reject'), ('in_progress', 'reject'), ('in_progress', 'complete')]:
            row = self.at_state(status)
            before = self.state()
            for invalid in ('', ' \t\n\r\u00a0\u2003', 'д' * 501, 'bad\x00text'):
                with self.subTest(status=status, action=action, length=len(invalid)):
                    with self.assertRaises(RequestError) as caught:
                        self.change(row, action, invalid)
                    self.assertIn('explanation', caught.exception.field_errors)
                    self.assertEqual(self.state(), before)
            self.change(row, action, 'д' * 500)
            row.refresh_from_db()
            self.assertEqual(row.events.get(version=row.version).explanation, 'д' * 500)

    def test_same_key_lost_response_replay_changed_payload_and_new_key(self):
        actor = self.actors()
        token = issue_submission(actor)
        first = self.create('  Текст\r\nзаявки ', token=token)  # pretend its response was lost
        replay = self.create('Текст\nзаявки', token=token)
        self.assertEqual(first['id'], replay['id'])
        self.assertTrue(first['created'])
        self.assertFalse(replay['created'])
        self.assertEqual(self.counts(), (1, 1, 1))
        accepted = self.state()
        for comment, kind in [('Другой', REQUEST_TYPE), ('я' * 501, REQUEST_TYPE), ('Текст\nзаявки', 'other')]:
            with self.assertRaises(RequestError) as caught:
                self.create(comment, token=token, request_type=kind)
            self.assertEqual(caught.exception.code, 'payload_conflict')
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(self.state(), accepted)
        new = self.create('Текст\nзаявки')
        self.assertNotEqual(new['id'], first['id'])
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_notification_failure_rolls_back_creation_and_transition(self):
        actor = self.actors()
        token = issue_submission(actor)
        with patch('notifications.services.record_request_event', side_effect=IntegrityError('isolated required notification failure')):
            with self.assertRaises(IntegrityError):
                self.create(token=token)
        self.assertEqual(self.counts(), (0, 0, 0))
        row = Request.objects.get(pk=self.create(token=token)['id'])
        before = self.state()
        with patch('notifications.services.record_request_event', side_effect=IntegrityError('isolated required notification failure')):
            with self.assertRaises(IntegrityError):
                self.change(row, 'take')
        self.assertEqual(self.state(), before)
        # Even a buggy writer silently omitting the call is rejected at commit.
        with patch('notifications.services.record_request_event', return_value=None):
            with self.assertRaises(IntegrityError):
                self.change(row, 'take')
        self.assertEqual(self.state(), before)

    def test_lost_staff_response_stale_version_does_not_repeat(self):
        self.actors()
        row = Request.objects.get(pk=self.create()['id'])
        self.change(row, 'take')
        before = self.state()
        with self.assertRaises(RequestError) as caught:
            self.change(row, 'take', version=1)
        self.assertEqual(caught.exception.code, 'version_conflict')
        self.assertEqual(self.state(), before)
        row.refresh_from_db()
        self.change(row, 'complete', 'Обработка демонстрации завершена.')
        before = self.state()
        with self.assertRaises(RequestError):
            self.change(row, 'complete', 'Повтор потерянного ответа.', version=2)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.counts(), (1, 3, 3))

    def test_database_payload_routing_history_and_version_constraints(self):
        self.actors()
        row = Request.objects.get(pk=self.create()['id'])
        before = self.state()
        from studies.models import StudentProfile
        foreign = StudentProfile.objects.get(source_id='SP-B')
        for change in ({'comment': 'Подмена'}, {'owner_id': foreign.pk}, {'version': 9},
                       {'status': 'ready', 'version': 2}, {'request_type': 'other'}):
            with self.subTest(change=change), self.assertRaises(IntegrityError), transaction.atomic():
                Request.objects.filter(pk=row.pk).update(**change)
        with self.assertRaises(IntegrityError), transaction.atomic():
            RequestEvent.objects.filter(request=row).update(explanation='Изменённая история')
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('DELETE FROM student_requests_requestevent WHERE request_id = %s', [row.pk])
        with self.assertRaises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute('DELETE FROM student_requests_request WHERE id = %s', [row.pk])
        self.assertEqual(self.state(), before)
        # A state update without its event is rejected by a deferred constraint.
        with self.assertRaises(IntegrityError), transaction.atomic():
            Request.objects.filter(pk=row.pk).update(status='in_progress', version=2)
        self.assertEqual(self.state(), before)

    def test_owner_context_binding_replays_and_student_forbidden_transition(self):
        actor = self.actors()
        token = issue_submission(actor)
        row = Request.objects.get(pk=self.create(token=token)['id'])
        before = self.state()
        self.login(self.b, 'student_b')
        with self.assertRaises(AccessDenied) as caught:
            self.create(token=token, client=self.b)
        self.assertEqual(caught.exception.code, 'stale_context')
        with self.assertRaises(AccessDenied) as caught:
            self.change(row, 'take', client=self.a)
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(AccessDenied) as caught:
            self.create(client=self.staff)
        self.assertEqual(caught.exception.status, 403)
        # Accepted key never bypasses a new login of the same owner.
        logout_session(self.request(), actor.marker)
        self.login()
        with self.assertRaises(AccessDenied) as caught:
            self.create(token=token)
        self.assertEqual(caught.exception.code, 'stale_context')
        self.assertEqual(self.state(), before)

    def test_replay_after_scope_revision_change_or_absolute_expiry_is_denied(self):
        actor = self.actors()
        token = issue_submission(actor)
        self.create(token=token)
        before = self.state()
        context = SessionContext.objects.get(pk=actor.context_id)
        preloaded_request = self.request()  # Начатый до expiry HTTP request уже загрузил session.
        with patch('django.utils.timezone.now', return_value=context.expires_at):
            with self.assertRaises(AccessDenied) as caught:
                submit_request(preloaded_request, actor.marker, token, REQUEST_TYPE, 'Для демонстрации')
            self.assertEqual(caught.exception.code, 'session_expired')
        batch = self.scope_change()
        apply(batch, expected_revision=4, operator='s5-test')
        with self.assertRaises(AccessDenied) as caught:
            self.create(token=token)
        self.assertEqual(caught.exception.code, 'stale_context')
        self.assertEqual(self.state(), before)
        # Fresh same-login context can submit; stable original routing survives.
        self.create()
        self.assertEqual(self.counts(), (2, 2, 2))

    def test_routing_stays_at_submission_and_current_staff_scope_controls_access(self):
        self.actors()
        original = Request.objects.get(pk=self.create()['id'])
        original_faculty = original.servicing_faculty_id
        payload = deepcopy(self.scope_change().payload)
        source = payload['source_as_of']
        # Разрешённая коррекция A остаётся внутри PR-MB. Другой факультет —
        # отдельный изолированный synthetic fixture, не перенос между программами.
        payload['operations'][:0] = [
            {'operation': 'create', 'entity_type': 'faculty', 'record': {
                'source_id': 'F-OTHER', 'record_revision': 1, 'source_updated_at': source,
                'name': 'Другой синтетический факультет'}},
            {'operation': 'create', 'entity_type': 'programme', 'record': {
                'source_id': 'PR-OTHER', 'record_revision': 1, 'source_updated_at': source,
                'name': 'Другая синтетическая программа', 'faculty_id': 'F-OTHER'}},
            {'operation': 'create', 'entity_type': 'group', 'record': {
                'source_id': 'G-OTHER', 'record_revision': 1, 'source_updated_at': source,
                'name': 'ДР-11 (демо)', 'programme_id': 'PR-OTHER'}},
            {'operation': 'create', 'entity_type': 'profile', 'record': {
                'source_id': 'SP-OTHER', 'record_revision': 1, 'source_updated_at': source,
                'name': 'Другой заявитель (демо)', 'group_id': 'G-OTHER', 'year': 1}},
        ]
        apply(validate(signed(payload)), expected_revision=4, operator='isolated-routing')
        original.refresh_from_db()
        self.assertEqual(original.servicing_faculty_id, original_faculty)
        self.assertEqual(original.owner.group.source_id, 'G-MB22')
        from studies.models import StudentProfile
        other_user = get_user_model().objects.create_user(
            username='student_other', password='Isolated-s5-student-password')
        Identity.objects.create(user=other_user, role='student',
            profile=StudentProfile.objects.get(source_id='SP-OTHER'))
        self.credentials['student_other'] = 'Isolated-s5-student-password'
        other_client = Client(enforce_csrf_checks=True)
        self.login(other_client, 'student_other')
        other = Request.objects.get(pk=self.create(client=other_client)['id'])
        self.assertNotEqual(other.servicing_faculty_id, original_faculty)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Request.objects.filter(pk=original.pk).update(servicing_faculty_id=other.servicing_faculty_id)
        staff_actor = current_actor(self.request(self.staff))
        listing = request_page(staff_actor)
        self.assertEqual(listing['request_count'], 1)
        self.assertEqual([row['id'] for row in listing['requests']], [original.pk])
        for operation in ('read', 'write'):
            with self.assertRaises(AccessDenied) as caught:
                if operation == 'read':
                    request_page(staff_actor, request_id=other.pk)
                else:
                    self.change(other, 'take')
            self.assertEqual(caught.exception.status, 404)
        self.change(original, 'take')
        original.refresh_from_db()
        # The first employee did not acquire exclusive ownership.
        password = 'Isolated-s5-staff-password'
        second = get_user_model().objects.create_user(username='staff_second', password=password)
        identity = Identity.objects.create(user=second, role='staff', display_name='Второй сотрудник (демо)')
        StaffScope.objects.create(identity=identity, faculty_id=original_faculty)
        self.credentials['staff_second'] = password
        client = Client(enforce_csrf_checks=True)
        self.login(client, 'staff_second')
        self.change(original, 'complete', 'Второй сотрудник завершил демонстрацию.', client=client)
        original.refresh_from_db()
        self.assertEqual(original.status, 'ready')

    def test_saved_detail_is_plain_text_and_new_submission_after_rejection_is_separate(self):
        actor = self.actors()
        text = '<script>alert("<student>")</script>'
        row = Request.objects.get(pk=self.create(text)['id'])
        self.change(row, 'reject', '<img src=x onerror=alert(1)> Причина отказа (демо).')
        row.refresh_from_db()
        data = request_page(actor, request_id=row.pk)
        self.assertEqual(data['request_item']['comment'], text)
        self.assertEqual(data['request_item']['status'], 'rejected')
        self.assertEqual(data['request_item']['explanation'], row.events.get(version=2).explanation)
        self.assertEqual(len(data['history']), 2)
        self.assertEqual(data['actions'], [])
        new = Request.objects.get(pk=self.create(text)['id'])
        self.assertNotEqual(row.reference, new.reference)
        self.assertEqual(new.status, 'submitted')

    def test_bootstrap_is_explicit_idempotent_and_retains_later_action(self):
        self.actors()
        source_before = canonical_state()
        first = bootstrap_examples(operator='isolated-test')
        self.assertEqual([item['created'] for item in first], [True, True])
        a = Request.objects.get(owner__source_id='SP-A')
        b = Request.objects.get(owner__source_id='SP-B')
        self.assertEqual((a.status, a.version, b.status, b.version), ('in_progress', 2, 'ready', 3))
        self.assertEqual(self.counts(), (2, 5, 5))
        self.change(a, 'reject', 'Позднейшее решение сохраняется.')
        before = self.state()
        repeated = bootstrap_examples(operator='isolated-test-again')
        self.assertEqual([item['created'] for item in repeated], [False, False])
        self.assertEqual(self.state(), before)
        self.assertEqual(canonical_state(), source_before)
        head = get_head()
        batch = SyntheticDatasetProvider().load(ROOT / 'fixtures/s2/r4.json')
        self.assertEqual(apply(batch, expected_revision=4, operator='s5-test')['outcome'], 'noop')
        check_integrity(head)
        self.assertEqual(self.state(), before)

    def concurrent(self, worker):
        held, release = threading.Event(), threading.Event()
        pids, results = {}, {}
        def run(name):
            def barrier(execute, sql, params, many, context):
                value = execute(sql, params, many, context)
                if name == 'first' and 'accounts_authorizationguard' in sql and 'FOR UPDATE' in sql:
                    held.set()
                    if not release.wait(8):
                        raise RuntimeError('barrier timeout')
                return value
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids[name] = cursor.fetchone()[0]
                with connections['default'].execute_wrapper(barrier):
                    results[name] = worker(name)
            except RequestError as exc:
                results[name] = {'error': exc.code, 'status': exc.status}
            except Exception as exc:
                results[name] = {'unexpected': type(exc).__name__ + ':' + str(exc)}
            finally:
                connections['default'].close()
        first = threading.Thread(target=run, args=('first',))
        second = threading.Thread(target=run, args=('second',))
        first.start()
        try:
            self.assertTrue(held.wait(5))
            second.start()
            blocked = False
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if 'second' in pids:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_blocking_pids(%s)', [pids['second']])
                        if pids['first'] in cursor.fetchone()[0]:
                            blocked = True
                            break
                time.sleep(.01)
            self.assertTrue(blocked, 'Второе независимое соединение должно ждать guard')
        finally:
            release.set()
            first.join(8)
            if second.ident:
                second.join(8)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertNotEqual(pids['first'], pids['second'])
        return results

    def test_simultaneous_same_key_creates_exactly_one_request_event_notification(self):
        actor = self.actors()
        token = issue_submission(actor)
        reqs = {name: self.request() for name in ('first', 'second')}
        results = self.concurrent(lambda name: submit_request(reqs[name], actor.marker, token, REQUEST_TYPE, 'Одновременная отправка'))
        self.assertEqual(results['first']['id'], results['second']['id'], results)
        self.assertTrue(results['first']['created'])
        self.assertFalse(results['second']['created'])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_competing_staff_decisions_have_one_winner(self):
        self.actors()
        row = self.at_state('in_progress')
        reqs = {name: self.request(self.staff) for name in ('first', 'second')}
        marker = current_actor(reqs['first']).marker
        results = self.concurrent(lambda name: transition_request(reqs[name], row.pk, marker,
            'complete' if name == 'first' else 'reject', 2, 'Решение ' + name))
        self.assertEqual(results['first']['id'], row.pk, results)
        self.assertEqual(results['second'], {'error': 'version_conflict', 'status': 409})
        row.refresh_from_db()
        self.assertEqual((row.status, row.version), ('ready', 3))
        self.assertEqual(self.counts(), (1, 3, 3))
