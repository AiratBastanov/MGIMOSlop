"""S5: два реальных PG-соединения и наблюдаемое ожидание locks.

Clock override применяется только в изолированных expiry-тестах после
подтверждённого ожидания; обычные заявки и прочие проверки используют now.
"""
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.db import connection, connections, transaction
from django.test import Client, RequestFactory, TransactionTestCase
from django.utils import timezone

from accounts.access import AccessDenied, current_actor, lock_guard, logout_session, set_active
from accounts.models import SessionContext
from accounts.provisioning import provision
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply, initialize
from notifications.models import Notification
from student_requests.models import Request, RequestEvent
from student_requests.services import issue_submission, submit_request, transition_request
from student_requests.workflow import REQUEST_TYPE


ROOT = Path(__file__).resolve().parents[1]


class RequestAccessRacesTests(TransactionTestCase):
    def setUp(self):
        initialize()
        for revision in range(1, 5):
            stage = 's1' if revision < 3 else 's2'
            batch = SyntheticDatasetProvider().load(ROOT / f'fixtures/{stage}/r{revision}.json')
            apply(batch, expected_revision=revision - 1, operator='s5-access-race')
        directory = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.addCleanup(directory.cleanup)
        credential_path = Path(directory.name) / 'credentials.json'
        provision(credential_path)
        credentials = json.loads(credential_path.read_text(encoding='utf-8'))
        self.student, self.staff = Client(enforce_csrf_checks=True), Client(enforce_csrf_checks=True)
        for client, username in ((self.student, 'student_a'), (self.staff, 'staff_demo')):
            client.get('/login/')
            response = client.post('/login/', {
                'username': username, 'password': credentials[username],
                'csrfmiddlewaretoken': client.cookies['csrftoken'].value,
            })
            self.assertEqual(response.status_code, 302)

    def request(self, client):
        request = RequestFactory().post('/requests/')
        request.session = SessionStore(session_key=client.cookies['sessionid'].value)
        # Начавшийся HTTP request уже загрузил session до ожидания guard.
        dict(request.session.items())
        return request

    def prepare_writer(self, kind):
        student_request = self.request(self.student)
        actor = current_actor(student_request)
        token = issue_submission(actor)
        self.request_id = None
        if kind == 'submit':
            def write():
                return submit_request(student_request, actor.marker, token,
                                      REQUEST_TYPE, 'Изолированная проверка гонки')
            return write, actor, self.student, 0
        created = submit_request(student_request, actor.marker, token,
                                 REQUEST_TYPE, 'Изолированная проверка гонки')
        self.request_id = created['id']
        if kind == 'replay':
            def replay():
                return submit_request(student_request, actor.marker, token,
                                      REQUEST_TYPE, 'Изолированная проверка гонки')
            return replay, actor, self.student, 1
        staff_request = self.request(self.staff)
        actor = current_actor(staff_request)

        def transition():
            return transition_request(staff_request, self.request_id, actor.marker, 'take', 1)
        return transition, actor, self.staff, 1

    def counts(self):
        return (Request.objects.count(), RequestEvent.objects.count(),
                Notification.objects.filter(request_event__isnull=False).count())

    def observe_blocked(self, pids, waiter, holder):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if waiter in pids and holder in pids:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT pg_blocking_pids(%s)', [pids[waiter]])
                    if pids[holder] in cursor.fetchone()[0]:
                        self.assertNotEqual(pids[holder], pids[waiter])
                        return
            time.sleep(.01)
        self.fail('Второе PostgreSQL-соединение не показало ожидание первого')

    def race(self, kind, access_change, first):
        write, actor, client, before_events = self.prepare_writer(kind)
        logout_request = self.request(client)
        held, release = threading.Event(), threading.Event()
        pids, results = {}, {}

        def work(name):
            def barrier(execute, sql, params, many, context):
                value = execute(sql, params, many, context)
                if (name == first and not held.is_set()
                        and 'accounts_authorizationguard' in sql and 'FOR UPDATE' in sql):
                    held.set()
                    if not release.wait(8):
                        raise RuntimeError('guard barrier timeout')
                return value
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids[name] = cursor.fetchone()[0]
                with connections['default'].execute_wrapper(barrier):
                    if name == 'writer':
                        results['saved'] = write()
                    elif access_change == 'logout':
                        logout_session(logout_request, actor.marker)
                    else:
                        set_active(actor.user_id, False, operator='isolated-s5-race')
                results[name] = 'ok'
            except AccessDenied as exc:
                results[name] = exc.code
            except Exception as exc:
                results[name] = type(exc).__name__ + ':' + str(exc)
            finally:
                connections['default'].close()

        second = 'access' if first == 'writer' else 'writer'
        holder = threading.Thread(target=work, args=(first,))
        waiter = threading.Thread(target=work, args=(second,))
        holder.start()
        try:
            self.assertTrue(held.wait(5), results)
            waiter.start()
            self.observe_blocked(pids, second, first)
        finally:
            release.set()
            holder.join(8)
            if waiter.ident:
                waiter.join(8)
        self.assertFalse(holder.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(results.get('access'), 'ok', results)
        self.assertEqual(results.get('writer'), 'ok' if first == 'writer' else 'session_expired', results)
        expected_events = before_events + (first == 'writer')
        expected_requests = 1 if kind == 'transition' or first == 'writer' else 0
        self.assertEqual(self.counts(), (expected_requests, expected_events, expected_events))
        if expected_requests:
            row = Request.objects.get()
            self.assertEqual(row.version, expected_events)
            self.assertEqual(row.status, 'in_progress' if expected_events == 2 else 'submitted')
            self.assertEqual(list(row.events.order_by('version').values_list('version', flat=True)),
                             list(range(1, expected_events + 1)))
        # Успевший commit остаётся, но этот context больше не годится для POST.
        with self.assertRaises(AccessDenied):
            write()
        self.assertEqual(self.counts(), (expected_requests, expected_events, expected_events))

    def test_submit_commit_before_logout_is_preserved(self):
        self.race('submit', 'logout', 'writer')

    def test_logout_before_submit_rejects_preloaded_session(self):
        self.race('submit', 'logout', 'access')

    def test_submit_commit_before_revocation_is_preserved(self):
        self.race('submit', 'revoke', 'writer')

    def test_revocation_before_submit_rejects_preloaded_session(self):
        self.race('submit', 'revoke', 'access')

    def test_transition_commit_before_logout_is_preserved(self):
        self.race('transition', 'logout', 'writer')

    def test_logout_before_transition_rejects_preloaded_session(self):
        self.race('transition', 'logout', 'access')

    def test_transition_commit_before_revocation_is_preserved(self):
        self.race('transition', 'revoke', 'writer')

    def test_revocation_before_transition_rejects_preloaded_session(self):
        self.race('transition', 'revoke', 'access')

    def expiry_while_waiting(self, kind, lock):
        write, actor, _, before_events = self.prepare_writer(kind)
        expiry = SessionContext.objects.get(pk=actor.context_id).expires_at
        before = self.counts()
        held, release, elapsed = threading.Event(), threading.Event(), threading.Event()
        pids, results = {}, {}
        real_now = timezone.now

        def clock():
            if threading.current_thread().name == 's5-expiry-writer' and elapsed.is_set():
                return expiry  # Равенство уже истекло; тестовый security clock.
            return real_now()

        def holder():
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids['holder'] = cursor.fetchone()[0]
                with transaction.atomic():
                    if lock == 'guard':
                        lock_guard()
                    else:
                        Request.objects.select_for_update().get(pk=self.request_id)
                    held.set()
                    if not release.wait(8):
                        raise RuntimeError('expiry barrier timeout')
                results['holder'] = 'ok'
            except Exception as exc:
                results['holder'] = type(exc).__name__ + ':' + str(exc)
            finally:
                connections['default'].close()

        def writer():
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids['writer'] = cursor.fetchone()[0]
                write()
                results['writer'] = 'unexpected_success'
            except AccessDenied as exc:
                results['writer'] = exc.code
            except Exception as exc:
                results['writer'] = type(exc).__name__ + ':' + str(exc)
            finally:
                connections['default'].close()

        blocker = threading.Thread(target=holder, name='s5-expiry-holder')
        blocked = threading.Thread(target=writer, name='s5-expiry-writer')
        with patch('accounts.access.timezone.now', side_effect=clock):
            blocker.start()
            try:
                self.assertTrue(held.wait(5), results)
                blocked.start()
                self.observe_blocked(pids, 'writer', 'holder')
                elapsed.set()
            finally:
                release.set()
                blocker.join(8)
                if blocked.ident:
                    blocked.join(8)
        self.assertFalse(blocker.is_alive())
        self.assertFalse(blocked.is_alive())
        self.assertEqual(results, {'holder': 'ok', 'writer': 'session_expired'})
        self.assertEqual(self.counts(), before)
        if before_events:
            row = Request.objects.get(pk=self.request_id)
            self.assertEqual((row.version, row.status), (1, 'submitted'))

    def test_submit_expiry_is_checked_after_guard_wait(self):
        self.expiry_while_waiting('submit', 'guard')

    def test_transition_expiry_is_checked_after_guard_wait(self):
        self.expiry_while_waiting('transition', 'guard')

    def test_transition_expiry_is_checked_after_target_wait(self):
        self.expiry_while_waiting('transition', 'target')

    def test_accepted_key_replay_checks_expiry_after_target_wait(self):
        self.expiry_while_waiting('replay', 'target')
