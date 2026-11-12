"""Независимые ожидания фиксированных r8/r9 на реальной S1–S3 lineage, только test PG."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, RequestFactory, TransactionTestCase

from accounts.provisioning import provision
from accounts.selectors import private_snapshot
from data_import.contracts import ImportRejected
from data_import.models import Dataset
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply, initialize, preview
from data_import.state import canonical_state
from notifications.models import Notification, NotificationRead
from studies.models import Enrollment, FinalResult, OutstandingRequirement, AssessmentEvent, Lesson

ROOT = Path(__file__).resolve().parents[1]


class S4FixedScenariosTests(TransactionTestCase):
    def setUp(self):
        initialize()
        self.provider = SyntheticDatasetProvider()
        for revision in range(1, 8):
            stage = 's1' if revision < 3 else 's2' if revision < 5 else 's3'
            self.publish(f'fixtures/{stage}/r{revision}.json')
        directory = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.addCleanup(directory.cleanup)
        credentials = Path(directory.name) / 'credentials.json'
        provision(credentials)
        self.credentials = json.loads(credentials.read_text(encoding='utf-8'))
        self.expected = json.loads((ROOT / 'fixtures/s4/expectations.json').read_text(encoding='utf-8'))
        self.a, self.b = Client(enforce_csrf_checks=True), Client(enforce_csrf_checks=True)
        for client, username in ((self.a, 'student_a'), (self.b, 'student_b')):
            client.get('/login/')
            response = client.post('/login/', {'username': username, 'password': self.credentials[username],
                'csrfmiddlewaretoken': client.cookies['csrftoken'].value})
            self.assertEqual(response.status_code, 302)

    def publish(self, path):
        batch = self.provider.load(ROOT / path)
        before = canonical_state()
        self.assertEqual(preview(batch, expected_revision=batch.parent or 0)['outcome'], 'preview')
        self.assertEqual(canonical_state(), before)
        return apply(batch, expected_revision=batch.parent or 0, operator='isolated-fixed-s4')

    def study(self, client=None, **kwargs):
        request = RequestFactory().get('/study/')
        request.session = SessionStore(session_key=(client or self.a).cookies['sessionid'].value)
        dict(request.session.items())
        # Будущая дата только для domain snapshot test; security clock настоящий.
        with patch('accounts.selectors.read_now', return_value=datetime.fromisoformat('2026-11-07T23:30:00+03:00')):
            return private_snapshot(request, section='study', **kwargs)

    def assert_counts(self, case):
        for student, client in (('SP-A', self.a), ('SP-B', self.b)):
            if student not in self.expected[case]:
                continue
            summary = self.study(client)['summary']
            self.assertTrue(summary['complete'])
            self.assertEqual({key: summary[key] for key in ('X', 'Y', 'Z', 'N')}, self.expected[case][student])

    def test_fixed_base_then_current_demo_numbers_history_and_real_assessments(self):
        legacy = canonical_state()
        lesson_count = Lesson.objects.count()
        self.publish('fixtures/s4/r8.json')
        self.assert_counts('r8')
        self.publish('fixtures/s4/r9.json')
        self.assert_counts('r9')
        self.assertEqual(Lesson.objects.count(), lesson_count)
        self.assertEqual(FinalResult.objects.get(source_id='FR-A-ECON').grade, 3)
        self.assertEqual(sorted(OutstandingRequirement.objects.filter(status='open').values_list('source_id', flat=True)), self.expected['r9_open_requirements'])
        self.assertEqual(sorted(OutstandingRequirement.objects.filter(status='closed').values_list('source_id', flat=True)), self.expected['r9_closed_requirements'])
        row = next(item for item in self.study()['courses'] if item['source_id'] == 'EN-A-ECON')
        self.assertEqual(row['result_label'], '3')
        self.assertEqual(len(row['open_requirements']), 2)
        self.assertTrue(any(item['due_label'] == 'Срок не указан' for item in row['open_requirements']))
        missing = next(item for item in self.study()['courses'] if item['source_id'] == 'EN-A-LAW')
        self.assertEqual(missing['result_label'], 'Итог не внесён')
        upcoming = self.study()['next_assessment']
        self.assertEqual(upcoming['source_id'], 'AS-A-LAW')
        self.assertEqual(upcoming['starts_at'], datetime.fromisoformat('2026-11-09T15:30:00+03:00'))
        self.assertEqual(upcoming['room'], '312')
        self.assertEqual(AssessmentEvent.objects.get(source_id='AS-A-LANG').status, 'cancelled')
        current = canonical_state()
        for section in ('revisions', 'coverage', 'changes', 'profile_changes', 'announcement_changes'):
            for item in legacy[section]:
                self.assertIn(item, current[section])
        before = canonical_state()
        replay = self.provider.load(ROOT / 'fixtures/s4/r9.json')
        self.assertEqual(apply(replay, expected_revision=9, operator='isolated-fixed-replay')['outcome'], 'noop')
        self.assertEqual(before, canonical_state())

    def test_separate_clean_correction_branch_has_four_positive_courses(self):
        self.publish('fixtures/s4/r8.json')
        self.publish('fixtures/s4/isolated/correction_clean_from_r8.json')
        self.assert_counts('correction_clean_from_r8')

    def test_invalid_synthetic_failure_rejected_before_publication(self):
        before = canonical_state()
        candidate = self.provider.load(ROOT / 'fixtures/s4/isolated/invalid_failure_without_requirement_from_r7.json')
        with self.assertRaises(ImportRejected):
            preview(candidate, expected_revision=7)
        with self.assertRaises(ImportRejected):
            apply(candidate, expected_revision=7, operator='isolated-invalid')
        self.assertEqual(canonical_state(), before)

    def test_partial_candidates_preview_nonmutating_and_complete_zero(self):
        self.publish('fixtures/s4/r8.json')
        self.publish('fixtures/s4/r9.json')
        baseline = canonical_state()
        for domain in ('enrollment', 'final_result', 'requirement', 'assessment'):
            candidate = self.provider.load(ROOT / f'fixtures/s4/isolated/partial_{domain}_from_r9.json')
            self.assertEqual(preview(candidate, expected_revision=9)['outcome'], 'preview')
            self.assertEqual(canonical_state(), baseline)
        self.publish('fixtures/s4/isolated/complete_zero_from_r9.json')
        summary = self.study(period_id='P-S4-EMPTY')['summary']
        self.assertTrue(summary['complete'])
        self.assertEqual({key: summary[key] for key in ('X', 'Y', 'Z', 'N')}, self.expected['complete_zero'])
