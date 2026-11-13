"""Связь подготовки с записью и выборка совпадений: отдельная PostgreSQL БД."""
from datetime import timedelta
import json
import uuid
from unittest.mock import patch
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.test import TransactionTestCase
from django.utils import timezone
from accounts.access import AccessDenied
from accounts.models import SessionContext
from consultations.models import Booking, ConsultationSlot, ConsultationEvent, ConsultationOperation
from consultations.services import BookingError, _event
from consultations.selectors import page_data, calendar_events
from notifications.models import Notification
from materials.models import TeachingAssignment
from studies.models import StudentProfile
from journeys.models import Journey, JourneyBooking
from journeys.selectors import journey_values
from discovery.catalog import actions
from discovery.catalog import parse_query, ranked, entry
from discovery.query import metadata
from django.db.models import Value
from tests.test_consultations import ConsultationTests
from tests.test_discovery import SearchAccessTests
from tests.test_journeys import JourneyTests


class ConnectedCorrectionTests(TransactionTestCase):
    setUp = ConsultationTests.setUp
    login = ConsultationTests.login
    request = ConsultationTests.request
    page = ConsultationTests.page
    post = ConsultationTests.post
    marker = ConsultationTests.marker
    actor = ConsultationTests.actor
    run_action = ConsultationTests.run_action
    start = ConsultationTests.start
    slot = ConsultationTests.slot
    book = ConsultationTests.book
    action = JourneyTests.action
    journey_start = JourneyTests.start
    publish = SearchAccessTests.publish
    search = SearchAccessTests.search
    create_request = SearchAccessTests.create_request
    staff = ConsultationTests.staff
    race = ConsultationTests.race

    def test_existing_manual_anchor_follows_transfer(self):
        journey = self.journey_start()
        first, _ = self.book(self.slot())
        self.action({'action': 'link_booking', 'booking': first.pk}, journey)
        target = self.slot(starts=self.start() + timedelta(hours=1))
        moved = self.run_action(action='rebook', booking=first.pk, version=first.version,
            slot=target.pk, slot_version=target.version, question='', academic_ack='1')
        value = journey_values(self.actor(), journey, timezone.now())
        self.assertEqual([row['id'] for row in value['bookings']], [moved['booking_id']])
        self.assertEqual(JourneyBooking.objects.get(journey=journey).booking_id, first.pk)
        second = Booking.objects.get(pk=moved['booking_id'])
        other = self.journey_start('correction')
        self.action({'action': 'link_booking', 'booking': first.pk}, other)
        self.action({'action': 'link_booking', 'booking': second.pk}, journey)
        target2 = self.slot(starts=self.start() + timedelta(hours=2))
        moved2 = self.run_action(action='rebook', booking=second.pk, version=second.version,
            slot=target2.pk, slot_version=target2.version, question='', academic_ack='1')
        third = Booking.objects.get(pk=moved2['booking_id'])
        for plan in (journey, other):
            value = journey_values(self.actor(), plan, timezone.now())['bookings']
            self.assertEqual([row['id'] for row in value], [third.pk])
            self.assertEqual([row['id'] for row in value[0]['previous_bookings']], [first.pk, second.pk])
            self.assertIn('Запись перенесена на другое время', json.dumps(value, ensure_ascii=False))
        self.assertEqual([row['id'] for row in calendar_events(self.actor(), timezone.now())], [third.pk])
        before = journey_values(self.actor(), journey, timezone.now())['bookings']
        target3 = self.slot(starts=self.start() + timedelta(hours=3))
        self.run_action(self.teacher, action='close', slot=target3.pk, version=target3.version)
        with self.assertRaises(BookingError):
            self.run_action(action='rebook', booking=third.pk, version=third.version,
                slot=target3.pk, slot_version=target3.version, academic_ack='1')
        self.assertEqual(journey_values(self.actor(), journey, timezone.now())['bookings'], before)
        self.run_action(action='cancel', booking=third.pk, version=third.version)
        value = journey_values(self.actor(), journey, timezone.now())['bookings']
        self.assertEqual([(row['id'], row['state']) for row in value], [(third.pk, 'cancelled')])
        self.assertFalse(calendar_events(self.actor(), timezone.now()))
        self.assertEqual(list(JourneyBooking.objects.filter(journey=journey).order_by('pk').values_list('booking_id', flat=True)), [first.pk, second.pk])

    def test_atomic_association_retry_owner_and_host_boundary(self):
        from journeys.booking_links import owned_journey
        for invalid in ('²', '9' * 5000, True, [], None, '0'):
            with self.assertRaises(AccessDenied):
                owned_journey(self.actor(), invalid)
        journey = self.journey_start()
        journey = JourneyTests.prepared(self, journey)
        slot = self.slot()
        fields = dict(action='book', slot=slot.pk, version=slot.version, journey=journey.pk,
                      question='Только вопрос ведущему', academic_ack='1', operation=str(uuid.uuid4()))
        counts = lambda: (Booking.objects.count(), JourneyBooking.objects.count(), ConsultationEvent.objects.count(),
                          ConsultationOperation.objects.count(), Notification.objects.count())
        before = counts()
        with patch('consultations.services.associate', side_effect=RuntimeError('controlled association failure')):
            with self.assertRaises(RuntimeError):
                self.run_action(**fields)
        self.assertEqual(counts(), before)
        result = self.run_action(**fields)
        after = counts()
        self.assertEqual(self.run_action(**fields), result)
        self.assertEqual(counts(), after)
        self.assertEqual(result['journey_url'], f'/journeys/{journey.pk}/')
        self.assertEqual(JourneyBooking.objects.get(journey=journey).booking_id, result['booking_id'])
        own = page_data(self.actor(), timezone.now(), {'journey': str(journey.pk)})
        self.assertEqual(own['journey_context']['url'], result['journey_url'])
        host = page_data(self.actor(self.teacher), timezone.now(), {}, result['booking_id'])
        exposed = json.dumps(host, ensure_ascii=False)
        self.assertIsNone(host['journey_context'])
        self.assertNotIn('НЕПЕРЕДАННАЯ ЗАМЕТКА', exposed)
        self.assertNotIn('/journeys/', exposed)
        self.assertIsNone(host['bookings'][0]['request'])
        self.assertEqual(self.teacher.get(f'/journeys/{journey.pk}/').status_code, 403)
        self.login(self.b, 'student_b')
        for client in (self.teacher, self.b):
            with self.assertRaises(AccessDenied):
                page_data(self.actor(client), timezone.now(), {'journey': str(journey.pk)})
        foreign_id = self.action({'action': 'start', 'template': 'exchange'}, client=self.b)['id']
        later = self.slot(starts=self.start() + timedelta(hours=1))
        bad = dict(fields, slot=later.pk, journey=foreign_id, operation=str(uuid.uuid4()))
        with self.assertRaises(AccessDenied):
            self.run_action(**bad)
        self.assertEqual(counts()[0:2], after[0:2])
        # Separate explicit faculty request consent; personal association grants none.
        request = self.create_request()
        with self.assertRaises(BookingError):
            self.run_action(**dict(bad, journey=journey.pk, request=request.pk))
        staff = self.staff()
        faculty_slot = self.slot(staff, starts=self.start() + timedelta(hours=2))
        shared, _ = self.book(faculty_slot, journey=journey.pk, request=request.pk)
        host = page_data(self.actor(staff), timezone.now(), {}, shared.pk)
        self.assertEqual(next(b for b in host['bookings'] if b['id'] == shared.pk)['request']['id'], request.pk)
        self.assertNotIn('НЕПЕРЕДАННАЯ ЗАМЕТКА', json.dumps(host, ensure_ascii=False))
        actor = self.actor()
        SessionContext.objects.filter(pk=actor.context_id).update(revoked_at=timezone.now())
        with self.assertRaises(AccessDenied):
            self.run_action(**fields)

    def test_endpoint_finds_title_after_old_candidate_prefix(self):
        rare, _ = self.publish(title='Ёлочный кванториум')
        for number in range(201):
            self.publish(title=f'Обычная публикация {number:03d}')
        data = self.search('елочн квантор')
        self.assertEqual([row['url'] for row in data['items']], [f'/study/teaching/{rare.pk}/'])
        self.assertFalse(data['limited'])
        self.publish(action='save', title='Ёлочный кванториум')
        foreign = TeachingAssignment.objects.create(teacher=self.assignment.teacher,
            course=self.assignment.course, period=self.assignment.period,
            group=StudentProfile.objects.get(source_id='SP-B').group,
            available_from=self.assignment.available_from, available_until=self.assignment.available_until)
        self.publish(assignment=foreign.pk, title='Ёлочный кванториум')
        self.assertEqual(self.search('елочн квантор'), data)
        broad = self.search('обычн публикац')
        self.assertTrue(broad['limited'])
        source = next(s for s in broad['sources'] if s['key'] == 'teaching')
        self.assertTrue(source['limited'])
        self.assertEqual(broad['count'], 200)
        self.assertIn('совпадений', source['message'])
        with CaptureQueriesContext(connection) as captured:
            narrow = self.search('обычн публикац 000')
        self.assertEqual(narrow['count'], 1)
        self.assertFalse(narrow['limited'])
        self.assertEqual(sum('LIMIT 201' in q['sql'] for q in captured), 9)
        queries = [q['sql'] for q in captured if 'workspaces_teachingpublication' in q['sql'] and 'LIMIT 201' in q['sql']]
        self.assertEqual(len(queries), 1)
        self.assertIn(' ~ ', queries[0])
        self.assertNotIn('"workspaces_teachingrevision"."payload",', queries[0])
        print('search endpoint measured queries:', len(captured), '; matching SQL before LIMIT 201; metadata projection only')
        next_page = self.search('обычн публикац', page='2')
        self.assertEqual(self.search('обычн публикац')['items'], broad['items'])
        self.assertTrue({r['key'] for r in broad['items']}.isdisjoint(r['key'] for r in next_page['items']))
        self.assertEqual(self.search('ЁЛОЧНЫЙ КВАНТОРИУМ')['items'], data['items'])
        self.assertEqual(self.search('справка')['items'][0]['key'], 'action:certificate')
        req = self.create_request()
        self.assertEqual([r['url'] for r in self.search(req.reference)['items']], [f'/requests/{req.pk}/'])
        self.login(self.b, 'student_b')
        self.assertEqual(self.search(req.reference, self.b)['count'], 0)

    def test_consultation_metadata_has_independent_matching_limits(self):
        # Disposable query-selection fixture; real transitions are exercised above.
        # No disable-trigger/reset and no writes to the demonstration database.
        student, teacher = self.actor(), self.actor(self.teacher)
        with transaction.atomic():
            slots = ConsultationSlot.objects.bulk_create([ConsultationSlot(host=teacher.identity, kind='teacher',
                assignment=self.assignment, starts_at=self.start() + timedelta(minutes=30 * n),
                ends_at=self.start() + timedelta(minutes=30 * n + 20), venue='Синтетическая аудитория',
                topic='Точность бронеплана' if n == 0 else 'Резерв квантума' if n == 201 else f'Консультация {n:03d}')
                for n in range(202)])
            for slot in slots:
                _event(teacher, slot, 'created')
            bookings = Booking.objects.bulk_create([Booking(student_id=student.profile_id, slot=slot,
                starts_at=slot.starts_at, ends_at=slot.ends_at) for slot in slots[:201]])
            for booking in bookings:
                _event(student, booking.slot, 'booked', booking)
        self.assertEqual([r['url'] for r in self.search('бронеплан')['items']], [f'/consultations/bookings/{bookings[0].pk}/'])
        self.assertEqual([r['url'] for r in self.search('квантум')['items']], [f'/consultations/?slot={slots[-1].pk}'])
        broad = self.search('консультация')
        self.assertTrue(broad['limited'])
        self.assertTrue(next(s for s in broad['sources'] if s['key'] == 'consultation_bookings')['limited'])
        self.assertFalse(next(s for s in broad['sources'] if s['key'] == 'consultation_slots')['limited'])
        self.login(self.b, 'student_b')
        self.assertEqual(self.search('квантум', self.b)['count'], 0)

    def test_all_grades_action_uses_assessed_results_contract(self):
        action = next(row for row in actions('student') if row['key'] == 'action:grades')
        self.assertEqual(action['url'], '/study/?view=results&period=all&filter=assessed')

    def test_sql_metadata_matches_catalog_normalization_and_word_boundaries(self):
        cases = [('ЁЛОЧНЫЙ кванториум', 'елочн квантор', True), ('Приём', 'консультация', True),
            ('Файл 0042', '42', False), ('Файл 42', '42', True), ('Документ прооценка', 'оценка', False),
            ('ＳＳＥＭＩＮＡＲ', 'ssemin', True), ('Straße', 'strass', True), ('ᲀвод', 'ввод', True),
            ('Обмен', 'learning agreement', True), ('Дисциплина', 'материал', False)]
        for title, raw, expected in cases:
            query, tokens, _ = parse_query(raw)
            values = dict(title=Value(title), category=Value('Запись'), context=Value('Краткая подпись'))
            rows = list(metadata(TeachingAssignment.objects.filter(pk=self.assignment.pk), tokens, **values))
            self.assertEqual(bool(rows), expected, (title, raw))
            self.assertEqual(bool(rows), bool(ranked([entry('one', title, 'Запись', 'Краткая подпись', '/')], query, tokens)))
        def expire(*args):
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '1ms'")
                cursor.execute('SELECT pg_sleep(0.02)')
        actor = self.actor()
        with patch('discovery.views.search', side_effect=expire):
            response = self.a.post('/search/results/', {'q': 'обмен', 'page': '1', 'context': actor.marker,
                'csrfmiddlewaretoken': self.a.cookies['csrftoken'].value}, HTTP_X_COMPONENT='1', HTTP_X_PAGE_CONTEXT=actor.marker)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['state'], 'unavailable')
        self.assertNotIn('items', response.json())
        with CaptureQueriesContext(connection) as measured:
            self.assertEqual(self.search('справка')['items'][0]['key'], 'action:certificate')
        print('final endpoint measured queries:', len(measured), '; scoped empty/coverage check included')
        from studies.models import Enrollment
        with patch('discovery.selectors.enrollment_scope', return_value=Enrollment.objects.none()):
            empty = self.search('справка')
        self.assertTrue(empty['partial'])
        self.assertFalse(empty['limited'])
        self.assertEqual(next(s for s in empty['sources'] if s['key']=='academic_coverage')['state'], 'partial')

    def test_concurrent_same_connected_intent_is_one_booking_and_one_link(self):
        journey = self.journey_start()
        slot = self.slot()
        fields = dict(action='book', slot=slot.pk, version=slot.version, journey=journey.pk,
                      question='', academic_ack='1', operation=str(uuid.uuid4()))
        result = self.race({'first': lambda: self.run_action(**fields),
                            'retry': lambda: self.run_action(**fields)}, 'first')
        self.assertIsInstance(result['first'], dict, result)
        self.assertEqual(result['first'], result['retry'])
        self.assertEqual(Booking.objects.count(), 1)
        self.assertEqual(JourneyBooking.objects.filter(journey=journey).count(), 1)
