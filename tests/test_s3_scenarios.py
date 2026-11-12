"""Независимые контрольные факты сохранённой fixed S3 fixture lineage, PostgreSQL."""
from copy import deepcopy
from datetime import date, datetime, time, timedelta
import json
from pathlib import Path
import tempfile

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from accounts.models import Identity, StaffScope
from accounts.provisioning import provision
from data_import.contracts import ImportRejected, MOSCOW, validate
from data_import.models import Dataset
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply, initialize, preview
from data_import.state import canonical_state
from notifications.models import Announcement, Notification, NotificationRead
from studies.models import Lesson, ScheduleChange
from tests.test_import import signed

ROOT = Path(__file__).resolve().parents[1]


class S3FixtureScenariosTests(TransactionTestCase):
    def setUp(self):
        initialize()
        for revision in range(1, 5):
            stage = 1 if revision < 3 else 2
            apply(SyntheticDatasetProvider().load(ROOT / f'fixtures/s{stage}/r{revision}.json'),
                  expected_revision=revision-1, operator='s3-fixture-test')
        temporary = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.addCleanup(temporary.cleanup)
        provision(Path(temporary.name) / 'credentials.json')
        self.legacy_notification = Notification.objects.get(schedule_change__event_id='EV-S2-ROOM')
        self.legacy_read = NotificationRead.objects.create(
            user=get_user_model().objects.get(username='student_a'), notification=self.legacy_notification)
        self.security = self.security_rows()
        self.legacy = {key: value for key, value in canonical_state().items() if key != 'records'}
        for revision in [5, 6, 7]:
            apply(SyntheticDatasetProvider().load(ROOT / f'fixtures/s3/r{revision}.json'),
                  expected_revision=revision-1, operator='s3-fixture-test')
        self.expected = json.loads((ROOT / 'fixtures/s3/expectations.json').read_text(encoding='utf-8'))

    def security_rows(self):
        return {
            'users': list(get_user_model().objects.order_by('pk').values()),
            'identity': list(Identity.objects.order_by('pk').values()),
            'staffscope': list(StaffScope.objects.order_by('pk').values()),
            'read': list(NotificationRead.objects.order_by('pk').values()),
        }

    def test_independent_fixed_facts_history_coverage_and_legacy_preservation(self):
        self.assertEqual(Dataset.objects.get(pk='s1-schedule').current_revision, self.expected['accepted_revision'])
        for source_id, fields in self.expected['preserve'].items():
            lesson = Lesson.objects.get(source_id=source_id)
            for key, value in fields.items():
                self.assertEqual(getattr(lesson, key), value, source_id)
        for source_id, fields in self.expected['lessons'].items():
            lesson = Lesson.objects.select_related('instructor').get(source_id=source_id)
            start, end = lesson.starts_at.astimezone(MOSCOW), lesson.ends_at.astimezone(MOSCOW)
            actual = dict(date=start.date().isoformat(), start=start.strftime('%H:%M'), end=end.strftime('%H:%M'),
                          room=lesson.room, record_revision=lesson.record_revision, status=lesson.status,
                          instructor_id=lesson.instructor.source_id)
            self.assertEqual(actual, fields, source_id)
        for group, days in self.expected['active_days'].items():
            for text, expected_ids in days.items():
                start = datetime.combine(date.fromisoformat(text), time.min, tzinfo=MOSCOW)
                ids = list(Lesson.objects.filter(group__source_id=group, status='active', starts_at__gte=start,
                    starts_at__lt=start+timedelta(days=1)).order_by('starts_at', 'source_id').values_list('source_id', flat=True))
                self.assertEqual(ids, expected_ids, (group, text))
        for source_id, expected_ids in self.expected['history'].items():
            history = ScheduleChange.objects.filter(lesson__source_id=source_id).order_by('revision__revision', 'event_id')
            self.assertEqual(list(history.values_list('event_id', flat=True)), expected_ids)
        chain = list(ScheduleChange.objects.filter(lesson__source_id='L-A-1123-1').order_by('revision__revision'))
        self.assertEqual(chain[0].before['room'], '201')
        self.assertEqual(chain[0].after['room'], '318')
        self.assertEqual(chain[1].before, chain[0].after)
        self.assertEqual(datetime.fromisoformat(chain[1].before['starts_at']).astimezone(MOSCOW).date(), date(2026,11,23))
        self.assertEqual(datetime.fromisoformat(chain[1].after['starts_at']).astimezone(MOSCOW).date(), date(2026,11,24))
        for source_id, status in self.expected['announcements'].items():
            self.assertEqual(Announcement.objects.get(source_id=source_id).status, status)
        for source_id, due in self.expected['deadlines'].items():
            self.assertEqual(Announcement.objects.get(source_id=source_id).due_at, datetime.fromisoformat(due))
        self.assertEqual(self.security_rows(), self.security)
        self.legacy_read.refresh_from_db()
        self.assertEqual(self.legacy_read.notification_id, self.legacy_notification.pk)
        current = canonical_state()
        for section in ['revisions', 'coverage', 'changes', 'profile_changes']:
            for row in self.legacy[section]:
                self.assertIn(row, current[section])
        before = canonical_state()
        result = apply(SyntheticDatasetProvider().load(ROOT / 'fixtures/s3/r7.json'), expected_revision=7, operator='s3-fixture-replay')
        self.assertEqual(result['outcome'], 'noop')
        self.assertEqual(canonical_state(), before)
        self.assertEqual(self.security_rows(), self.security)

    def test_isolated_candidates_preview_without_changing_demo_and_retained_coverage(self):
        before = canonical_state()
        for name in ['partial', 'unknown', 'optional_fields']:
            result = preview(SyntheticDatasetProvider().load(ROOT / f'fixtures/s3/isolated/{name}.json'), expected_revision=7)
            self.assertEqual(result['outcome'], 'preview')
            self.assertEqual(canonical_state(), before)
        candidate = json.loads((ROOT / 'fixtures/s3/r7.json').read_text(encoding='utf-8'))
        candidate.update(dataset_revision=8, parent_revision=7, operations=[])
        for coverage in candidate['coverage']:
            if coverage['group_id'] == 'G-MB21':
                coverage['active_ids'].remove('L-S2-23')
                coverage['active_count'] -= 1
        with self.assertRaises(ImportRejected):
            preview(validate(signed(candidate)), expected_revision=7)
        self.assertEqual(canonical_state(), before)
        self.assertEqual(self.security_rows(), self.security)
