"""Записи: настоящие PostgreSQL транзакции, отдельные соединения и авторизация."""
import json
import threading
import time
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection, connections, transaction, IntegrityError
from django.test import Client, TransactionTestCase
from django.utils import timezone

from accounts.access import current_actor, AccessDenied
from accounts.models import Identity, StaffScope
from accounts.selectors import domain_notifications
from consultations.models import ConsultationSlot, Booking, ConsultationEvent, ConsultationOperation
from consultations.selectors import available_slots, booking_scope, booking_values, calendar_events, conflict_state, page_data
from consultations.services import perform, BookingError
from data_import.contracts import validate
from data_import.models import Dataset
from data_import.services import apply
from data_import.state import canonical_state, records
from materials.services import set_assignment_active
from notifications.models import Notification, NotificationRead, NotificationReceipt, EmailOutbox
from profiles.models import Preferences
from studies.models import StudentProfile, Lesson
from student_requests.models import Request
from student_requests.services import issue_submission, submit_request
from student_requests.workflow import REQUEST_TYPE
from tests import test_task10_materials as legacy
from tests.test_auth import ROOT
from tests.test_import import signed


class ConsultationTests(TransactionTestCase):
    setUp = legacy.MaterialTests.setUp
    login = legacy.MaterialTests.login
    request = legacy.MaterialTests.request
    page = legacy.MaterialTests.page
    post = legacy.MaterialTests.post
    marker = legacy.MaterialTests.marker

    def staff(self):
        client = Client(enforce_csrf_checks=True)
        self.login(client, 'staff_demo')
        return client

    def actor(self, client=None):
        return current_actor(self.request(client))

    def run_action(self, client=None, **data):
        data.setdefault('operation', str(uuid.uuid4()))
        request = self.request(client)
        return perform(request, current_actor(request).marker, data)

    def start(self):
        return (timezone.now() + timedelta(days=1)).replace(hour=18, minute=10, second=0, microsecond=0)

    def slot(self, client=None, starts=None, minutes=20, **extra):
        client = client or self.teacher
        actor = self.actor(client)
        audience = self.assignment.pk if actor.role == 'teacher' else StaffScope.objects.filter(identity=actor.identity).first().faculty_id
        start = starts or self.start()
        result = self.run_action(client, action='create', audience=str(audience), starts_at=start.isoformat(),
            ends_at=(start + timedelta(minutes=minutes)).isoformat(), topic=extra.pop('topic', 'Синтетическая консультация'),
            venue='Демонстрационная аудитория', meeting_url='', **extra)
        return ConsultationSlot.objects.get(pk=result['slot_id'])

    def book(self, slot, client=None, **extra):
        data = {'action': 'book', 'slot': str(slot.pk), 'version': str(slot.version),
                'question': 'Личный синтетический вопрос', 'academic_ack': '1', 'operation': str(uuid.uuid4())}
        data.update(extra)
        result = self.run_action(client, **data)
        return Booking.objects.get(pk=result['booking_id']), data

    def import_operations(self, operations, *, add_lesson=None):
        batch = json.loads((ROOT / 'fixtures/s4/r9.json').read_text(encoding='utf-8'))
        revision = Dataset.objects.get().current_revision
        batch.update(dataset_revision=revision + 1, parent_revision=revision, source_as_of=timezone.now().isoformat(), operations=operations)
        if add_lesson:
            coverage = next(row for row in batch['coverage'] if row['group_id'] == add_lesson['group_id'])
            coverage['active_ids'] = sorted(coverage['active_ids'] + [add_lesson['source_id']])
            coverage['active_count'] += 1
        apply(validate(signed(batch)), expected_revision=revision, operator='isolated-consultation-test')

    def eligible_student(self):
        before_b = records()['profile']['SP-B']
        template = records()['profile']['SP-A']
        profile = dict(template, source_id='SP-CONSULT-C', record_revision=1,
                       source_updated_at=timezone.now().isoformat(), name='Вымышленный участник гонки')
        self.import_operations([{'operation': 'create', 'entity_type': 'profile', 'record': profile}])
        user = get_user_model().objects.create_user('consultation_student_c', password='isolated-consultation-only')
        Identity.objects.create(user=user, role='student', profile=StudentProfile.objects.get(source_id=profile['source_id']))
        self.credentials[user.username] = 'isolated-consultation-only'
        client = Client(enforce_csrf_checks=True)
        self.login(client, user.username)
        self.assertEqual(records()['profile']['SP-B'], before_b)
        return client

    def race(self, operations, first):
        held, release = threading.Event(), threading.Event()
        pids, outcomes = {}, {}
        second = next(name for name in operations if name != first)
        def run(name):
            def barrier(execute, sql, params, many, context):
                value = execute(sql, params, many, context)
                if name == first and not held.is_set() and 'accounts_authorizationguard' in sql and 'FOR UPDATE' in sql:
                    held.set()
                    if not release.wait(8):
                        raise RuntimeError('barrier timeout')
                return value
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()')
                    pids[name] = cursor.fetchone()[0]
                with connections['default'].execute_wrapper(barrier):
                    outcomes[name] = operations[name]()
            except BookingError:
                outcomes[name] = 'unavailable'
            except Exception as exc:
                outcomes[name] = repr(exc)
            finally:
                connections['default'].close()
        a, b = threading.Thread(target=run, args=(first,)), threading.Thread(target=run, args=(second,))
        a.start()
        try:
            self.assertTrue(held.wait(5))
            b.start()
            deadline, blocked = time.monotonic() + 5, False
            while time.monotonic() < deadline:
                if second in pids:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_blocking_pids(%s)', [pids[second]])
                        blocked = pids[first] in cursor.fetchone()[0]
                    if blocked:
                        break
                time.sleep(.01)
            self.assertTrue(blocked, pids)
            self.assertNotEqual(pids[first], pids[second])
        finally:
            release.set()
            a.join(8)
            if b.ident:
                b.join(8)
        self.assertFalse(a.is_alive() or b.is_alive())
        return outcomes

    def test_booking_persists_authorized_no_get_writes_and_notifications(self):
        official = canonical_state()
        slot = self.slot()
        student = self.actor()
        course = page_data(student, student.checked_at, {'course': str(self.assignment.course_id)})
        self.assertEqual([row['id'] for row in course['slots']], [slot.pk])
        self.assertIn('course=' + str(self.assignment.course_id), course['state_url'])
        self.login(self.b, 'student_b')
        for scoped_actor, course_id in ((student, '2147483647'), (self.actor(self.b), str(self.assignment.course_id))):
            with self.assertRaises(AccessDenied) as missing:
                page_data(scoped_actor, scoped_actor.checked_at, {'course': course_id})
            self.assertEqual((missing.exception.code, missing.exception.status), ('not_found', 404))
        Preferences.objects.update_or_create(user_id=student.user_id, defaults={'email_enabled': True,
            'contact_email': 'consultation@example.invalid', 'email_verified_at': timezone.now(), 'email_academic': True})
        booking, _ = self.book(slot)
        before = (Booking.objects.count(), ConsultationEvent.objects.count(), NotificationReceipt.objects.count(), NotificationRead.objects.count())
        for client in (self.a, self.teacher):
            response = client.get(f'/consultations/bookings/{booking.pk}/')
            self.assertEqual(response.status_code, 200, response.content[:300])
            self.assertEqual(response['Cache-Control'], 'private, no-store, max-age=0')
        self.assertEqual(before, (Booking.objects.count(), ConsultationEvent.objects.count(), NotificationReceipt.objects.count(), NotificationRead.objects.count()))
        self.assertEqual(self.b.get(f'/consultations/bookings/{booking.pk}/').status_code, 404)
        self.assertEqual(self.staff().get(f'/consultations/bookings/{booking.pk}/').status_code, 404)
        self.assertEqual(available_slots(student, timezone.now()).filter(pk=slot.pk).count(), 0)
        self.assertEqual(booking_scope(self.actor(self.b), timezone.now()).count(), 0)
        event = Notification.objects.get(application_event__consultation_event__booking=booking,
                                         application_event__consultation_event__action='booked')
        self.assertTrue(domain_notifications(student, now=timezone.now()).filter(pk=event.pk).exists())
        self.assertTrue(EmailOutbox.objects.filter(notification=event, user_id=student.user_id, status='queued').exists())
        self.assertEqual(canonical_state(), official)
        self.assertEqual(calendar_events(student, timezone.now())[0]['kind'], 'consultation')

    def test_postgres_capacity_overlap_and_immutable_time(self):
        slot = self.slot()
        with self.assertRaises(BookingError):
            self.slot(starts=slot.starts_at + timedelta(minutes=10))
        touching = self.slot(starts=slot.ends_at)
        one, _ = self.book(slot)
        two, _ = self.book(touching)
        self.assertEqual(Booking.objects.filter(state='booked').count(), 2)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ConsultationSlot.objects.filter(pk=slot.pk).update(starts_at=slot.starts_at + timedelta(minutes=1), version=slot.version + 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Booking.objects.create(slot=slot, student=one.student, starts_at=slot.starts_at, ends_at=slot.ends_at)
        # Другой host не снимает ограничение пересечений самого студента.
        staff_slot = self.slot(self.staff(), starts=slot.starts_at + timedelta(minutes=5))
        with self.assertRaises(BookingError):
            self.book(staff_slot)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Booking.objects.create(slot=staff_slot, student=one.student, starts_at=staff_slot.starts_at, ends_at=staff_slot.ends_at)
        self.assertEqual(Booking.objects.filter(state='booked').count(), 2)

    def test_two_real_connections_last_slot_and_duplicate_retry(self):
        other = self.eligible_student()
        slot = self.slot(self.staff())
        requests = {'a': self.request(), 'c': self.request(other)}
        markers = {name: current_actor(request).marker for name, request in requests.items()}
        data = {name: {'action': 'book', 'operation': str(uuid.uuid4()), 'slot': str(slot.pk), 'version': str(slot.version),
                       'question': '', 'academic_ack': '1'} for name in requests}
        outcome = self.race({name: (lambda n=name: perform(requests[n], markers[n], data[n])) for name in requests}, 'a')
        self.assertEqual(outcome['a']['state'], 'saved', outcome)
        self.assertEqual(outcome['c'], 'unavailable', outcome)
        self.assertEqual(Booking.objects.filter(slot=slot, state='booked').count(), 1)
        before = ConsultationEvent.objects.count()
        replay = perform(requests['a'], markers['a'], data['a'])
        self.assertEqual(replay, outcome['a'])
        self.assertEqual(ConsultationEvent.objects.count(), before)

    def test_cancel_rebook_race_both_serial_orders(self):
        staff = self.staff()
        for number, first in enumerate(('cancel', 'rebook')):
            old_slot = self.slot(staff, starts=self.start() + timedelta(hours=number * 3))
            target = self.slot(staff, starts=old_slot.ends_at + timedelta(minutes=5))
            old, _ = self.book(old_slot)
            request = self.request()
            marker = current_actor(request).marker
            cancel = {'action': 'cancel', 'operation': str(uuid.uuid4()), 'booking': str(old.pk), 'version': str(old.version)}
            rebook = {'action': 'rebook', 'operation': str(uuid.uuid4()), 'booking': str(old.pk), 'version': str(old.version),
                      'slot': str(target.pk), 'slot_version': str(target.version), 'question': '', 'academic_ack': '1'}
            outcome = self.race({'cancel': lambda: perform(request, marker, cancel),
                                 'rebook': lambda: perform(request, marker, rebook)}, first)
            self.assertEqual(outcome[first]['state'], 'saved', outcome)
            self.assertEqual(outcome['rebook' if first == 'cancel' else 'cancel'], 'unavailable', outcome)
            old.refresh_from_db()
            self.assertEqual(old.state, 'cancelled')
            self.assertEqual(Booking.objects.filter(slot=target, state='booked').count(), int(first == 'rebook'))

    def test_transfer_failure_preserves_old_booking_and_events(self):
        other = self.eligible_student()
        staff = self.staff()
        old_slot = self.slot(staff)
        target = self.slot(staff, starts=old_slot.ends_at)
        old, _ = self.book(old_slot)
        occupied, _ = self.book(target, other)
        before = (old.state, old.version, old.events.count())
        data = {'action': 'rebook', 'booking': str(old.pk), 'version': str(old.version), 'slot': str(target.pk),
                'slot_version': str(target.version), 'question': '', 'academic_ack': '1'}
        with self.assertRaises(BookingError):
            self.run_action(**data)
        old.refresh_from_db()
        self.assertEqual(before, (old.state, old.version, old.events.count()))
        self.run_action(other, action='cancel', booking=str(occupied.pk), version=str(occupied.version))
        with patch('consultations.services._event', side_effect=RuntimeError('isolated event fault')):
            with self.assertRaises(RuntimeError):
                self.run_action(**data)
        old.refresh_from_db()
        self.assertEqual(before, (old.state, old.version, old.events.count()))
        moved = self.run_action(**data)
        self.assertEqual(Booking.objects.get(pk=moved['booking_id']).replaces_id, old.pk)

    def test_retry_after_clock_advance_is_not_attendance_and_current_access_remains(self):
        start = (timezone.now() + timedelta(minutes=6)).replace(second=0, microsecond=0)
        slot = self.slot(starts=start, minutes=5)
        booking, data = self.book(slot)
        request = self.request()
        marker = current_actor(request).marker
        with patch('django.utils.timezone.now', return_value=slot.ends_at + timedelta(minutes=1)):
            replay = perform(request, marker, data)
            booking.refresh_from_db()
            self.assertEqual(replay['booking_id'], booking.pk)
            self.assertEqual(booking.state, 'booked')
            self.assertEqual(booking.events.count(), 1)
            with self.assertRaises(AccessDenied):
                self.run_action(action='complete', booking=str(booking.pk), version=str(booking.version))
            self.run_action(self.teacher, action='complete', booking=str(booking.pk), version=str(booking.version))
        booking.refresh_from_db()
        self.assertEqual(booking.state, 'completed')

    def test_cancelled_expired_revoked_and_current_ownership(self):
        slot = self.slot()
        booking, _ = self.book(slot)
        set_assignment_active(self.assignment.pk, False, operator='isolated-consultation-revocation')
        actor = self.actor()
        self.assertFalse(available_slots(actor, actor.checked_at).filter(pk=slot.pk).exists())
        self.assertFalse(booking_values(actor, Booking.objects.select_related('slot').get(pk=booking.pk), actor.checked_at)['audience_current'])
        self.assertEqual(self.teacher.get(f'/consultations/bookings/{booking.pk}/').status_code, 404)
        cancel = {'action': 'cancel', 'booking': str(booking.pk), 'version': str(booking.version), 'operation': str(uuid.uuid4())}
        self.run_action(**cancel)
        self.run_action(**cancel)
        self.assertEqual(ConsultationEvent.objects.filter(booking=booking, action='cancelled').count(), 1)
        staff = self.staff()
        closed = self.slot(staff)
        self.run_action(staff, action='close', slot=str(closed.pk), version=str(closed.version))
        with self.assertRaises(BookingError):
            self.book(closed)
        soon = (timezone.now() + timedelta(minutes=5)).replace(second=0, microsecond=0)
        expired = self.slot(staff, starts=soon, minutes=5)
        with patch('django.utils.timezone.now', return_value=expired.starts_at):
            with self.assertRaises(BookingError):
                self.book(expired)

    def test_known_academic_conflict_explicit_ack_partial_coverage_and_new_import(self):
        staff = self.staff()
        actor = self.actor()
        lesson = Lesson.objects.filter(group_id=actor.group_id, status='active', starts_at__gt=timezone.now()).order_by('starts_at').first()
        overlap = self.slot(staff, starts=lesson.starts_at, minutes=20)
        conflict = conflict_state(actor, actor.checked_at, overlap.starts_at, overlap.ends_at)
        self.assertTrue(conflict['academic'])
        with self.assertRaises(BookingError):
            self.book(overlap, academic_ack='')
        accepted, _ = self.book(overlap)
        self.assertTrue(accepted.academic_ack)
        later = self.slot(staff, starts=self.start() + timedelta(hours=1))
        booking, _ = self.book(later)
        old_lesson = next(iter(records()['lesson'].values()))
        added = dict(old_lesson, source_id='L-CONSULT-NEW', record_revision=1, group_id=actor.identity.profile.group.source_id,
            course_id=self.assignment.course.source_id, period_id=self.assignment.period.source_id,
            starts_at=later.starts_at.isoformat(), ends_at=later.ends_at.isoformat(), instructor_id=None,
            instructor_status='not_provided', source_updated_at=timezone.now().isoformat(), status='active')
        self.import_operations([{'operation': 'create', 'entity_type': 'lesson', 'record': added}], add_lesson=added)
        fresh = self.actor()
        booking.refresh_from_db()
        self.assertEqual(booking.state, 'booked')
        self.assertTrue(booking_values(fresh, booking, fresh.checked_at)['slot']['conflict']['academic'])
        future = self.slot(staff, starts=self.start() + timedelta(days=40))
        state = conflict_state(fresh, fresh.checked_at, future.starts_at, future.ends_at)
        self.assertFalse(state['complete'])
        self.assertIn('подтверждено не полностью', state['message'])

    def test_request_context_only_shared_explicitly_with_authorized_faculty_host(self):
        actor = self.actor()
        request = self.request()
        result = submit_request(request, actor.marker, issue_submission(actor), REQUEST_TYPE, 'Синтетический административный вопрос')
        case = Request.objects.get(pk=result['id'])
        teacher_slot = self.slot()
        with self.assertRaises(BookingError):
            self.book(teacher_slot, request=str(case.pk))
        staff = self.staff()
        staff_slot = self.slot(staff)
        booking, _ = self.book(staff_slot, request=str(case.pk))
        value = booking_values(self.actor(staff), booking, timezone.now())
        self.assertEqual(value['request']['id'], case.pk)
        self.assertEqual(self.teacher.get(f'/consultations/bookings/{booking.pk}/').status_code, 404)
        self.login(self.b, 'student_b')
        self.assertEqual(self.b.get(f'/consultations/?request={case.pk}').status_code, 404)
        self.assertEqual(page_data(self.actor(self.b), timezone.now(), {})['booking_count'], 0)

    def test_venue_change_cancel_notification_and_schema_requires_history(self):
        slot = self.slot()
        booking, _ = self.book(slot)
        self.run_action(self.teacher, action='venue', slot=str(slot.pk), version=str(slot.version), venue='Демо 202', meeting_url='')
        slot.refresh_from_db()
        booking.refresh_from_db()
        self.assertEqual(booking.slot.starts_at, slot.starts_at)
        self.assertTrue(NotificationReceipt.objects.filter(user_id=self.actor().user_id,
            notification__application_event__consultation_event__booking=booking,
            notification__application_event__consultation_event__action='venue').exists())
        with self.assertRaises(IntegrityError), transaction.atomic():
            ConsultationSlot.objects.filter(pk=slot.pk).update(version=slot.version + 1, venue='Недоказанная правка')
        self.run_action(self.teacher, action='cancel_slot', slot=str(slot.pk), version=str(slot.version))
        booking.refresh_from_db()
        self.assertEqual(booking.state, 'cancelled')
        self.assertEqual(booking.events.filter(action='slot_cancelled').count(), 1)
        from notifications.selectors import inbox_events, inbox_page
        student=self.actor()
        latest=Notification.objects.get(application_event__consultation_event__booking=booking,
            application_event__consultation_event__action='slot_cancelled')
        thread=inbox_events(student,now=timezone.now()).filter(application_event__consultation_event__booking=booking)
        self.assertEqual(list(thread.values_list('pk',flat=True)),[latest.pk])
        for kind in ('all','schedule'):
            self.assertIn(latest.pk,[row.pk for row in inbox_page(student,now=timezone.now(),kind=kind)['events']])
        self.login(self.b,'student_b')
        self.assertNotIn(latest.pk,[row.pk for row in inbox_page(self.actor(self.b),now=timezone.now(),kind='schedule')['events']])

    def test_calendar_midnight_uses_half_open_day_overlap(self):
        from data_import.contracts import MOSCOW
        start=self.start().astimezone(MOSCOW).replace(hour=23,minute=50)
        slot=self.slot(starts=start)
        booking,_=self.book(slot)
        following=(start+timedelta(days=1)).date()
        response=self.a.get('/schedule/',{'view':'day','date':following.isoformat()})
        self.assertEqual(response.status_code,200)
        self.assertEqual([event['id'] for event in response.context['days'][0]['consultations']],[booking.pk])
        self.assertIn(f'{following:%d.%m} 00:10',response.context['days'][0]['consultations'][0]['time_label'])
        boundary=self.slot(starts=start+timedelta(days=1,minutes=-10))
        self.book(boundary)
        after=(start+timedelta(days=2)).date()
        response=self.a.get('/schedule/',{'view':'day','date':after.isoformat()})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['days'][0]['consultations'],[])

    def test_demo_prepare_noop_preserves_changes_and_dates(self):
        from consultations.demo import provision
        result = provision(self.credentials_path, operator='isolated-consultation-demo')
        self.assertEqual(result['state'], 'created')
        booking = Booking.objects.get(pk=result['booking'])
        self.run_action(action='cancel', booking=str(booking.pk), version=str(booking.version))
        before = (list(ConsultationSlot.objects.values_list('pk', 'starts_at')), ConsultationEvent.objects.count())
        repeated = provision(self.credentials_path, operator='isolated-consultation-repeat')
        self.assertEqual(repeated['state'], 'noop')
        booking.refresh_from_db()
        self.assertEqual(booking.state, 'cancelled')
        self.assertEqual(before, (list(ConsultationSlot.objects.values_list('pk', 'starts_at')), ConsultationEvent.objects.count()))
