"""S3: отдельная PostgreSQL БД, независимые кандидаты и проверяемые переходы."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from accounts.models import AuthorizationGuard
from data_import.contracts import DATASET_ID, ImportRejected, content_hash, instant, validate
from data_import.models import Dataset, ImportRevision
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply, check_integrity, get_head, initialize, preview
from data_import.state import canonical_state, records, state_hash
from notifications.models import Announcement, AnnouncementChange, Notification, NotificationRead
from studies.models import Lesson, ScheduleChange

ROOT = Path(__file__).resolve().parents[1]


def signed(payload):
    payload['content_sha256'] = content_hash(payload)
    return validate(payload)


class S3ImportTests(TransactionTestCase):
    def setUp(self):
        initialize()
        provider = SyntheticDatasetProvider()
        for revision in range(1, 5):
            apply(provider.load(ROOT / f'fixtures/s{1 if revision < 3 else 2}/r{revision}.json'),
                  expected_revision=revision - 1, operator='s3-test')
        self.source = timezone.now().isoformat()
        self.legacy_notification = Notification.objects.get(schedule_change__event_id='EV-S2-ROOM')
        self.user = get_user_model().objects.create_user(username='s3-control', password='local-test-only')
        self.legacy_read = NotificationRead.objects.create(user=self.user, notification=self.legacy_notification)

    def candidate(self, operations, revision=5, coverage=None):
        value = json.loads((ROOT / 'fixtures/s2/r4.json').read_text(encoding='utf-8'))
        value.update(schema_version=3, subset='s3_planner_v3', dataset_revision=revision,
                     parent_revision=revision - 1, source_as_of=self.source, operations=operations,
                     prepared_by='Isolated S3 tests', preparation_reason='Изолированная проверка; не рабочий набор')
        if coverage is not None:
            value['coverage'] = coverage
        return value

    def announcement(self, source_id='AN-TEST', audience='all_students', **extra):
        row = dict(source_id=source_id, record_revision=1, source_updated_at=self.source,
                   title='Вымышленное объявление', body='Проверяемый простой текст без HTML.', audience=audience,
                   faculty_id=None, group_id=None, student_id=None,
                   published_at='2026-11-07T08:00:00+03:00', visible_until='2026-12-07T00:00:00+03:00',
                   due_at='2026-11-30T18:00:00+03:00', status='published')
        row.update(extra)
        return dict(operation='create', entity_type='announcement', event_id='EV-' + source_id, record=row)

    def publish(self, payload):
        return apply(signed(payload), expected_revision=payload['parent_revision'], operator='s3-test')

    def assert_legacy(self):
        lesson = Lesson.objects.get(source_id='L-S2-23')
        self.assertEqual((lesson.room, lesson.record_revision), ('312', 2))
        change = ScheduleChange.objects.get(event_id='EV-S2-ROOM')
        self.assertEqual((change.before['room'], change.after['room']), ('210', '312'))
        self.assertEqual(Notification.objects.get(schedule_change=change).pk, self.legacy_notification.pk)
        self.assertEqual(NotificationRead.objects.get(pk=self.legacy_read.pk).notification_id, self.legacy_notification.pk)

    def test_upgrade_preview_all_audiences_and_replay_preserve_old_hashes_reads(self):
        old = canonical_state(2)
        self.assertEqual(set(old), {'records', 'revisions', 'coverage', 'changes', 'notifications', 'profile_changes'})
        self.assertNotIn('announcement', old['records'])
        self.assertEqual(set(old['notifications'][0]), {'schedule_change__event_id', 'group__source_id', 'published_at'})
        self.assertEqual(state_hash(2), Dataset.objects.get().state_hash)
        guard_before = AuthorizationGuard.objects.get().revision
        payload = self.candidate([
            self.announcement('AN-ALL'),
            self.announcement('AN-FAC', 'faculty', faculty_id='F-S2'),
            self.announcement('AN-GROUP', 'group', group_id='G-MB21'),
            self.announcement('AN-STUDENT', 'student', student_id='SP-B'),
        ])
        batch = signed(payload)
        self.assertEqual(len(preview(batch, expected_revision=4)['changes']), 4)
        self.assertEqual(canonical_state(2), old)
        self.assertEqual(AuthorizationGuard.objects.get().revision, guard_before)
        self.assertFalse(Announcement.objects.exists())
        self.publish(payload)
        self.assertEqual((Announcement.objects.count(), AnnouncementChange.objects.count(), Notification.objects.count()), (4, 4, 6))
        self.assertEqual(set(Announcement.objects.values_list('audience', flat=True)), {'all_students','faculty','group','student'})
        self.assertEqual(Announcement.objects.get(source_id='AN-STUDENT').student.source_id, 'SP-B')
        self.assertEqual(Announcement.objects.get(source_id='AN-GROUP').group.source_id, 'G-MB21')
        self.assert_legacy()
        before = canonical_state()
        self.assertEqual(apply(batch, expected_revision=5, operator='s3-test')['outcome'], 'noop')
        self.assertEqual(canonical_state(), before)
        self.assert_legacy()
        check_integrity(get_head())

    def test_invalid_audience_reference_interval_enums_events_and_stale_parent_are_atomic(self):
        original = self.candidate([self.announcement()])
        cases = []
        def case(edit):
            value = deepcopy(original)
            edit(value)
            cases.append(value)
        case(lambda p: p['operations'][0]['record'].update(audience='faculty'))
        case(lambda p: p['operations'][0]['record'].update(audience='all_students', student_id='SP-A'))
        case(lambda p: p['operations'][0]['record'].update(audience='student', student_id='MISSING'))
        case(lambda p: p['operations'][0]['record'].update(audience='faculty', faculty_id='G-MB21'))
        case(lambda p: p['operations'][0]['record'].update(audience=[]))
        case(lambda p: p['operations'][0]['record'].update(status='archived'))
        case(lambda p: p['operations'][0]['record'].update(status='withdrawn'))
        case(lambda p: p['operations'][0]['record'].update(visible_until='2026-11-07T08:00:00+03:00'))
        case(lambda p: p['operations'][0]['record'].update(due_at='2026-11-07T07:59:59+03:00'))
        case(lambda p: p['operations'][0]['record'].update(published_at='2026-11-07T08:00:00'))
        case(lambda p: p['operations'][0]['record'].update(role='staff'))
        case(lambda p: p['operations'][0].update(event_id='EV-S2-ROOM'))
        case(lambda p: p['operations'].append(dict(deepcopy(p['operations'][0]), record=dict(p['operations'][0]['record'], source_id='AN-SECOND'))))
        case(lambda p: p.update(dataset_revision=6, parent_revision=5))
        case(lambda p: p.update(source_as_of='2099-01-01T00:00:00+00:00'))
        before = canonical_state()
        guard_before = AuthorizationGuard.objects.get().revision
        for index, value in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(ImportRejected):
                apply(signed(value), expected_revision=4, operator='s3-test')
            self.assertEqual(canonical_state(), before)
            self.assertEqual(AuthorizationGuard.objects.get().revision, guard_before)
            self.assert_legacy()

    def announcement_change(self, before, change_type, event, **changed):
        after = dict(before, record_revision=before['record_revision'] + 1, source_updated_at=self.source, **changed)
        return dict(operation='change', entity_type='announcement', event_id=event,
                    expected_record_revision=before['record_revision'], change_type=change_type,
                    before=before, after=after, reason='Изолированное изменение объявления')

    def test_draft_publication_correction_withdrawal_chain_and_read_not_remapped(self):
        self.publish(self.candidate([self.announcement(status='draft')]))
        draft = records()['announcement']['AN-TEST']
        initial_notification = Notification.objects.get(announcement_change__event_id='EV-AN-TEST')
        self.publish(self.candidate([self.announcement_change(draft, 'publish', 'EV-PUBLISH', status='published')], revision=6))
        published = records()['announcement']['AN-TEST']
        published_notification = Notification.objects.get(announcement_change__event_id='EV-PUBLISH')
        read = NotificationRead.objects.create(user=self.user, notification=published_notification)
        guard = AuthorizationGuard.objects.get().revision
        self.publish(self.candidate([self.announcement_change(published, 'correction', 'EV-CORRECT', title='Уточнённое объявление')], revision=7))
        corrected = records()['announcement']['AN-TEST']
        self.publish(self.candidate([self.announcement_change(corrected, 'withdraw', 'EV-WITHDRAW', status='withdrawn')], revision=8))
        self.assertEqual(Announcement.objects.get().status, 'withdrawn')
        self.assertEqual(AnnouncementChange.objects.count(), 4)
        self.assertEqual(Notification.objects.filter(announcement_change__isnull=False).count(), 4)
        self.assertEqual(NotificationRead.objects.get(pk=read.pk).notification_id, published_notification.pk)
        self.assertTrue(Notification.objects.filter(pk=initial_notification.pk).exists())
        chain = list(AnnouncementChange.objects.order_by('revision__revision').values_list('change_type', 'after__record_revision'))
        self.assertEqual(chain, [('create',1),('publish',2),('correction',3),('withdraw',4)])
        self.assertEqual(AnnouncementChange.objects.get(event_id='EV-CORRECT').before['title'], 'Вымышленное объявление')
        self.assertEqual(AuthorizationGuard.objects.get().revision, guard + 2)
        before = canonical_state()
        withdrawn = records()['announcement']['AN-TEST']
        with self.assertRaises(ImportRejected):
            self.publish(self.candidate([self.announcement_change(withdrawn, 'publish', 'EV-RESURRECT', status='published')], revision=9))
        self.assertEqual(canonical_state(), before)
        self.assert_legacy()

    def test_announcement_failure_after_required_notification_rolls_back_guard_and_history(self):
        self.publish(self.candidate([self.announcement()]))
        original = records()['announcement']['AN-TEST']
        value = self.candidate([self.announcement_change(original, 'withdraw', 'EV-ROLLBACK', status='withdrawn')], revision=6)
        before = canonical_state()
        guard_before = AuthorizationGuard.objects.get().revision
        def fail(change, *, published_at):
            Notification.objects.create(announcement_change=change, published_at=published_at)
            raise IntegrityError('isolated required notification failure')
        with patch('data_import.services.record_announcement_change', side_effect=fail), self.assertRaises(IntegrityError):
            self.publish(value)
        self.assertEqual(canonical_state(), before)
        self.assertEqual(AuthorizationGuard.objects.get().revision, guard_before)
        self.assert_legacy()

    def test_database_constraints_and_new_history_are_immutable(self):
        self.publish(self.candidate([self.announcement()]))
        for updates in [dict(audience='student'), dict(group_id=Lesson.objects.get(source_id='L-S2-23').group_id),
                        dict(status='bad'), dict(visible_until='2026-11-07T07:00:00+03:00')]:
            with self.subTest(updates=updates), self.assertRaises(IntegrityError), transaction.atomic():
                Announcement.objects.update(**updates)
        event = AnnouncementChange.objects.get()
        for operation in ['update','delete']:
            with self.subTest(operation=operation), self.assertRaises(IntegrityError), transaction.atomic():
                if operation == 'update':
                    AnnouncementChange.objects.filter(pk=event.pk).update(reason='unauthorized')
                else:
                    # DELETE would also be protected by its Notification FK; the trigger
                    # is tested directly with an UPDATE above.
                    AnnouncementChange.objects.filter(pk=event.pk)._raw_delete('default')
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(published_at=timezone.now())

    def test_schema3_cannot_adopt_unimported_new_source_rows(self):
        row = self.announcement()['record']
        Announcement.objects.create(dataset_id=DATASET_ID, **row)
        with self.assertRaisesRegex(ImportRejected, 'пусты'):
            self.publish(self.candidate([]))
        self.assertEqual(Dataset.objects.get().current_revision, 4)

    def test_move_substitute_cancel_chain_keeps_identity_and_validates_retained_coverage(self):
        lesson = records()['lesson']['L-S2-23']
        original_pk = Lesson.objects.get(source_id='L-S2-23').pk
        def lesson_change(before, kind, event, **changed):
            after = dict(before, record_revision=before['record_revision'] + 1, source_updated_at=self.source, **changed)
            return dict(operation='change', entity_type='lesson', event_id=event, change_type=kind,
                        expected_record_revision=before['record_revision'], before=before, after=after,
                        reason='Изолированная цепочка расписания')
        room = lesson_change(lesson, 'correction', 'EV-T-ROOM', room='410')
        self.publish(self.candidate([room]))
        coverage = deepcopy(self.candidate([])['coverage'])
        coverage[-1].update(active_ids=[], active_count=0)
        coverage.append(dict(coverage[-1], date_from='2026-11-24', date_until='2026-11-25', active_ids=['L-S2-23'], active_count=1))
        move = lesson_change(records()['lesson']['L-S2-23'], 'move', 'EV-T-MOVE',
                             starts_at='2026-11-24T09:00:00+03:00', ends_at='2026-11-24T10:30:00+03:00')
        self.publish(self.candidate([move], revision=6, coverage=coverage))
        instructor = dict(operation='create', entity_type='instructor', record=dict(source_id='I-S3-TEST',
                          record_revision=1, source_updated_at=self.source, name='Вымышленный преподаватель'))
        substitute = lesson_change(records()['lesson']['L-S2-23'], 'substitute', 'EV-T-SUBSTITUTE', instructor_id='I-S3-TEST')
        self.publish(self.candidate([instructor, substitute], revision=7, coverage=coverage))
        cancel = lesson_change(records()['lesson']['L-S2-23'], 'cancel', 'EV-T-CANCEL', status='cancelled')
        coverage[-1].update(active_ids=[], active_count=0)
        self.publish(self.candidate([cancel], revision=8, coverage=coverage))
        current = Lesson.objects.get(source_id='L-S2-23')
        self.assertEqual((current.pk, current.status, current.room, current.instructor.source_id),
                         (original_pk, 'cancelled', '410', 'I-S3-TEST'))
        changes = list(ScheduleChange.objects.filter(lesson=current).order_by('revision__revision'))
        self.assertEqual([c.event_id for c in changes], ['EV-S2-ROOM','EV-T-ROOM','EV-T-MOVE','EV-T-SUBSTITUTE','EV-T-CANCEL'])
        self.assertEqual(changes[2].before['starts_at'], '2026-11-23T09:20:00+00:00')
        self.assertEqual(changes[2].after['starts_at'], '2026-11-24T06:00:00+00:00')
        self.assertEqual(Notification.objects.filter(schedule_change__lesson=current).count(), 5)
        self.assertTrue(NotificationRead.objects.filter(pk=self.legacy_read.pk).exists())
        check_integrity(get_head())

    def test_optional_fields_are_explicit_and_complete_cannot_omit_retained_lesson(self):
        existing = records()['lesson']['L-S2-23']
        after = dict(existing, record_revision=3, source_updated_at=self.source, room=None,
                     location_status='not_provided', instructor_id=None, instructor_status='not_provided')
        operation = dict(operation='change', entity_type='lesson', expected_record_revision=2,
                         event_id='EV-OPTIONAL', change_type='correction', before=existing, after=after,
                         reason='Изолированные явно неизвестные поля')
        value = self.candidate([operation])
        self.publish(value)
        self.assertEqual((Lesson.objects.get(source_id='L-S2-23').room, Lesson.objects.get(source_id='L-S2-23').instructor_id), (None, None))
        bad = self.candidate([], revision=6)
        bad['coverage'][-1].update(active_ids=[], active_count=0)
        before = canonical_state()
        with self.assertRaisesRegex(ImportRejected, 'итоговыми активными'):
            self.publish(bad)
        self.assertEqual(canonical_state(), before)

    def test_datetime_offset_overflow_is_typed_rejection_without_writes(self):
        before = canonical_state()
        for timestamp in ('0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00',
                          '9999-12-31T23:59:59+00:00'):
            with self.subTest(timestamp=timestamp):
                # Третий момент представим в UTC, но не в Europe/Moscow.
                with self.assertRaises(ImportRejected) as error:
                    instant(timestamp)
                self.assertEqual(error.exception.code, 'timestamp')
                candidate = self.candidate([self.announcement(published_at=timestamp)])
                with self.assertRaises(ImportRejected):
                    self.publish(candidate)
                self.assertEqual(canonical_state(), before)
        self.assertEqual(instant('2026-11-23T12:20:00+03:00').isoformat(), '2026-11-23T09:20:00+00:00')
