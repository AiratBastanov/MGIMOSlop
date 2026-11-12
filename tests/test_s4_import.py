"""S4: независимые численные примеры, версия источника и атомарный writer."""
from copy import deepcopy
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from accounts.models import AuthorizationGuard, Identity, SessionContext, StaffScope
from data_import.contracts import DATASET_ID, ImportRejected
from data_import.models import AcademicCoverage, Dataset, ImportRevision
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply, check_integrity, get_head, initialize, preview
from data_import.state import canonical_state, records, state_hash
from notifications.models import Notification, NotificationRead
from studies.models import (AssessmentChange, AssessmentEvent, Enrollment, EnrollmentChange,
    Course, FinalResult, FinalResultChange, OutstandingRequirement, Period, RequirementChange, StudentProfile)

from .s4_fixtures import (ROOT, PERIOD, academic_coverage, build_payload, change, coverage, create, signed)


class S4ImportTests(TransactionTestCase):
    def setUp(self):
        initialize()
        provider = SyntheticDatasetProvider()
        for revision in range(1, 5):
            apply(provider.load(ROOT / f'fixtures/s{1 if revision < 3 else 2}/r{revision}.json'),
                  expected_revision=revision - 1, operator='s4-test')
        self.source = timezone.now().isoformat()
        self.control = get_user_model().objects.create_user(username='s4-control', password='local-test-only')
        self.old_notification = Notification.objects.get(schedule_change__event_id='EV-S2-ROOM')
        self.old_read = NotificationRead.objects.create(user=self.control, notification=self.old_notification)

    def publish(self, payload):
        return apply(signed(payload), expected_revision=payload['parent_revision'], operator='s4-test')

    def initial(self):
        value = build_payload(self.source)
        self.publish(value)
        return value

    def followup(self, operations, **kwargs):
        return build_payload(self.source, operations, revision=Dataset.objects.get().current_revision + 1, **kwargs)

    def protected(self):
        # Academic scope revisions and AccessAudit are deliberately not identity.
        return {'users':list(get_user_model().objects.order_by('pk').values()),
            'identities':list(Identity.objects.order_by('pk').values()),
            'staff_scopes':list(StaffScope.objects.order_by('pk').values()),
            'sessions':list(SessionContext.objects.order_by('pk').values()),
            'reads':list(NotificationRead.objects.order_by('pk').values())}

    def assert_rejected_unchanged(self, payload):
        before, protected = canonical_state(), self.protected()
        guard = AuthorizationGuard.objects.get().revision
        scopes = list(StudentProfile.objects.order_by('pk').values_list('scope_revision', flat=True))
        with self.assertRaises(ImportRejected):
            self.publish(payload)
        self.assertEqual(canonical_state(), before)
        self.assertEqual(self.protected(), protected)
        self.assertEqual(AuthorizationGuard.objects.get().revision, guard)
        self.assertEqual(list(StudentProfile.objects.order_by('pk').values_list('scope_revision', flat=True)), scopes)

    def test_s3_upgrade_preview_exact_head_preserves_legacy_hash_shapes_security_reads(self):
        provider = SyntheticDatasetProvider()
        for revision in range(5, 8):
            apply(provider.load(ROOT / f'fixtures/s3/r{revision}.json'), expected_revision=revision - 1, operator='s4-test')
        old = canonical_state(3)
        self.assertEqual(state_hash(3), Dataset.objects.get().state_hash)
        self.assertNotIn('enrollment', old['records'])
        self.assertNotIn('academic_coverage', old)
        old_revisions = list(ImportRevision.objects.order_by('revision').values())
        protected = self.protected()
        schedule_coverage = json.loads((ROOT / 'fixtures/s3/r7.json').read_text(encoding='utf-8'))['coverage']
        payload = build_payload(self.source, revision=8, schedule_coverage=schedule_coverage)
        self.assertEqual(preview(signed(payload), expected_revision=7)['outcome'], 'preview')
        self.assertEqual(canonical_state(3), old)
        self.assertFalse(Enrollment.objects.exists())
        self.publish(payload)
        self.assertEqual(list(ImportRevision.objects.filter(revision__lte=7).order_by('revision').values()), old_revisions)
        new_legacy = canonical_state(3)
        # New courses legitimately extend the legacy current-record table.
        for kind in old['records']:
            for source_id, row in old['records'][kind].items():
                self.assertEqual(new_legacy['records'][kind][source_id], row)
        for section in ['changes','notifications','profile_changes','announcement_changes','announcement_notifications']:
            self.assertEqual(new_legacy[section], old[section])
        self.assertEqual(self.protected(), protected)
        accepted = canonical_state()
        self.assertEqual(apply(signed(payload), expected_revision=8, operator='s4-test')['outcome'], 'noop')
        self.assertEqual(canonical_state(), accepted)
        self.assertEqual(self.protected(), protected)
        check_integrity(get_head())

    def test_numeric_fixture_raw_facts_and_explicit_correction_close_only_named_requirement(self):
        self.initial()
        self.assertEqual(list(FinalResult.objects.filter(enrollment__student__source_id='SP-A')
            .order_by('source_id').values_list('result_kind','grade','pass_status')),
            [('grade',5,None), ('grade',4,None), ('pass_fail',None,'passed'), ('grade',2,None)])
        self.assertEqual(Enrollment.objects.filter(student__source_id='SP-A').count(), 5)
        self.assertEqual(Enrollment.objects.filter(student__source_id='SP-B').count(), 4)
        self.assertFalse(FinalResult.objects.filter(enrollment__source_id='E-A-5').exists())
        before = records()
        operations = [change('final_result', before['final_result']['R-A-4'], self.source, 'EV-CORRECT', grade=3),
            change('requirement', before['requirement']['Q-A-4'], self.source, 'EV-CLOSE', change_type='close',
                status='closed', closed_at=self.source)]
        self.publish(self.followup(operations))
        self.assertEqual((FinalResult.objects.get(source_id='R-A-4').grade,
            OutstandingRequirement.objects.get(source_id='Q-A-4').status), (3,'closed'))
        self.assertEqual(FinalResultChange.objects.get(event_id='EV-CORRECT').before['grade'], 2)
        self.assertEqual(FinalResultChange.objects.get(event_id='EV-CORRECT').after['grade'], 3)
        self.assertEqual(RequirementChange.objects.get(event_id='EV-CLOSE').after['status'], 'closed')
        self.assertEqual(Notification.objects.filter(final_result_change__event_id='EV-CORRECT').count(), 1)
        self.assertEqual(Notification.objects.filter(requirement_change__event_id='EV-CLOSE').count(), 1)
        self.assertTrue(NotificationRead.objects.filter(pk=self.old_read.pk, notification=self.old_notification).exists())

    def test_positive_result_keeps_other_multiple_requirements_and_missing_due_time(self):
        self.initial()
        before = records()
        operations = [
            create('requirement', self.source, 'Q-OTHER', enrollment_id='E-A-4', result_id=None,
                reason='Не представлен обязательный документ для аттестации (демо).', status='open',
                opened_at=self.source, due_at=None, closed_at=None),
            create('requirement', self.source, 'Q-SECOND', enrollment_id='E-A-4', result_id=None,
                reason='Не завершён обязательный лабораторный допуск (демо).', status='open',
                opened_at=self.source, due_at=None, closed_at=None),
            change('final_result', before['final_result']['R-A-4'], self.source, 'EV-CORRECT', grade=3),
            change('requirement', before['requirement']['Q-A-4'], self.source, 'EV-CLOSE', change_type='close',
                status='closed', closed_at=self.source),
        ]
        scopes = academic_coverage()
        scopes[2] = coverage('requirement', ['Q-A-4','Q-OTHER','Q-SECOND'])
        self.publish(self.followup(operations, academic_coverage_value=scopes))
        self.assertEqual(FinalResult.objects.get(source_id='R-A-4').grade, 3)
        self.assertEqual(list(OutstandingRequirement.objects.filter(status='open').order_by('source_id')
            .values_list('source_id','due_at')), [('Q-OTHER',None),('Q-SECOND',None)])

    def test_grade_correction_does_not_implicitly_close_corresponding_requirement(self):
        self.initial()
        before = records()['final_result']['R-A-4']
        self.publish(self.followup([change('final_result', before, self.source, 'EV-GRADE-ONLY', grade=3)]))
        self.assertEqual(OutstandingRequirement.objects.get(source_id='Q-A-4').status, 'open')
        self.assertEqual(RequirementChange.objects.count(), 1)

    def test_result_requirement_consistency_validates_whole_batch_and_retained_records(self):
        initial = build_payload(self.source)
        without_requirement = deepcopy(initial)
        without_requirement['operations'] = [op for op in without_requirement['operations'] if op['entity_type'] != 'requirement']
        without_requirement['academic_coverage'][2] = coverage('requirement', [])
        self.assert_rejected_unchanged(without_requirement)
        self.publish(initial)
        row = records()['requirement']['Q-A-4']
        self.assert_rejected_unchanged(self.followup([change('requirement', row, self.source, 'EV-BAD-CLOSE',
            change_type='close', status='closed', closed_at=self.source)]))
        self.assert_rejected_unchanged(self.followup([change('requirement', row, self.source, 'EV-BAD-LINK', result_id=None)]))
        before = records()['final_result']['R-B-1']
        self.assert_rejected_unchanged(self.followup([change('final_result', before, self.source, 'EV-BAD-GRADE', grade=2)]))

    def test_invalid_values_ownership_period_references_and_unknown_fields_are_atomic(self):
        original = build_payload(self.source)
        cases = []
        def case(kind, field, value):
            payload = deepcopy(original)
            next(op['record'] for op in payload['operations'] if op['entity_type'] == kind)[field] = value
            cases.append(payload)
        for grade in [0, 1, 6, True, '4', None]:
            case('final_result', 'grade', grade)
        case('final_result', 'pass_status', 'passed')
        case('final_result', 'recorded_at', '2099-01-01T00:00:00+00:00')
        case('final_result', 'enrollment_id', 'MISSING')
        case('requirement', 'enrollment_id', 'E-B-1')
        case('requirement', 'closed_at', self.source)
        case('requirement', 'opened_at', '2099-01-01T00:00:00+00:00')
        case('requirement', 'reason', '')
        case('enrollment', 'student_id', 'MISSING')
        case('enrollment', 'period_id', 'MISSING')
        case('enrollment', 'role', 'staff')
        case('assessment', 'assessment_kind', 'homework')
        case('assessment', 'location_status', 'not_provided')
        case('assessment', 'instructor_id', 'MISSING')
        case('assessment', 'starts_at', '2026-12-09T14:00:00+03:00')
        for index, payload in enumerate(cases):
            with self.subTest(index=index):
                self.assert_rejected_unchanged(payload)

    def test_unique_enrollment_and_result_and_event_identity(self):
        original = build_payload(self.source)
        for kind in ['enrollment','final_result','assessment']:
            payload = deepcopy(original)
            duplicate = deepcopy(next(op for op in payload['operations'] if op['entity_type'] == kind))
            duplicate['record']['source_id'] += '-EXTRA'
            duplicate['event_id'] += '-EXTRA'
            payload['operations'].append(duplicate)
            with self.subTest(kind=kind):
                self.assert_rejected_unchanged(payload)
        payload = deepcopy(original)
        next(op for op in payload['operations'] if op['entity_type'] == 'enrollment')['event_id'] = 'EV-S2-ROOM'
        self.assert_rejected_unchanged(payload)

    def test_partial_domains_and_complete_empty_are_independent_of_schedule(self):
        scopes = [coverage(kind, completeness='partial') for kind in ['enrollment','final_result','requirement','assessment']]
        self.publish(build_payload(self.source, [], academic_coverage_value=scopes))
        self.assertEqual(list(AcademicCoverage.objects.order_by('entity_type').values_list('completeness', flat=True)), ['partial'] * 4)
        self.assertEqual(Enrollment.objects.count(), 0)
        empty = [coverage(kind, []) for kind in ['enrollment','final_result','requirement','assessment']]
        self.publish(self.followup([], academic_coverage_value=empty))
        self.assertEqual(list(AcademicCoverage.objects.filter(revision__revision=6).values_list('record_count', flat=True)), [0] * 4)
        self.assertEqual(OutstandingRequirement.objects.count(), 0)

    def test_complete_coverage_rejects_hidden_retained_records_foreign_scope_and_duplicates(self):
        self.initial()
        for index in range(4):
            scopes = academic_coverage()
            scopes[index].update(record_ids=[], record_count=0)
            self.assert_rejected_unchanged(self.followup([], academic_coverage_value=scopes))
        scopes = academic_coverage()
        scopes[0].update(student_id='SP-B')
        self.assert_rejected_unchanged(self.followup([], academic_coverage_value=scopes))
        scopes = academic_coverage()
        scopes.append(deepcopy(scopes[0]))
        self.assert_rejected_unchanged(self.followup([], academic_coverage_value=scopes))
        scopes = academic_coverage()
        scopes[0].update(completeness='partial')
        self.assert_rejected_unchanged(self.followup([], academic_coverage_value=scopes))

    def test_assessment_change_chain_identity_nulls_no_inferred_result_or_requirement(self):
        self.initial()
        before_count = (FinalResult.objects.count(), OutstandingRequirement.objects.count())
        target = AssessmentEvent.objects.get(source_id='AS-A-1')
        before = records()['assessment']['AS-A-1']
        self.publish(self.followup([change('assessment', before, self.source, 'EV-AS-MOVE', change_type='move',
            starts_at='2026-11-11T16:00:00+03:00', ends_at='2026-11-11T17:30:00+03:00', room='312')]))
        moved = records()['assessment']['AS-A-1']
        self.publish(self.followup([change('assessment', moved, self.source, 'EV-AS-UNKNOWN',
            instructor_id=None, instructor_status='not_provided', room=None, location_status='not_provided')]))
        unknown = records()['assessment']['AS-A-1']
        self.publish(self.followup([change('assessment', unknown, self.source, 'EV-AS-CANCEL', change_type='cancel', status='cancelled')]))
        target.refresh_from_db()
        self.assertEqual((target.pk, target.record_revision, target.status, target.room, target.instructor_id),
            (AssessmentEvent.objects.get(source_id='AS-A-1').pk, 4, 'cancelled', None, None))
        self.assertEqual(list(AssessmentChange.objects.filter(assessment=target).order_by('revision__revision')
            .values_list('change_type', flat=True)), ['create','move','correction','cancel'])
        self.assertEqual((FinalResult.objects.count(), OutstandingRequirement.objects.count()), before_count)
        self.assertEqual(Notification.objects.filter(assessment_change__assessment=target).count(), 4)

    def test_required_notifications_failures_rollback_facts_history_scope_guard_head(self):
        self.initial()
        for kind, field in [('final_result','grade'), ('requirement','reason'), ('assessment','room')]:
            row = next(iter(records()[kind].values()))
            replacement = 3 if kind == 'final_result' else 'Изменённое значение (демо)'
            payload = self.followup([change(kind, row, self.source, 'EV-ROLLBACK-' + kind, **{field:replacement})])
            before, protected = canonical_state(), self.protected()
            guard = AuthorizationGuard.objects.get().revision
            def fail(history, *, published_at):
                Notification.objects.create(**{kind + '_change':history}, published_at=published_at)
                raise IntegrityError('isolated required notification failure')
            with self.subTest(kind=kind), patch('data_import.services.record_' + kind + '_change', side_effect=fail), self.assertRaises(IntegrityError):
                self.publish(payload)
            self.assertEqual(canonical_state(), before)
            self.assertEqual(self.protected(), protected)
            self.assertEqual(AuthorizationGuard.objects.get().revision, guard)

    def test_enrollment_scope_revision_and_guard_change_without_rewriting_protected_state(self):
        protected = self.protected()
        before_scopes = dict(StudentProfile.objects.values_list('source_id','scope_revision'))
        self.initial()
        self.assertEqual(dict(StudentProfile.objects.values_list('source_id','scope_revision')),
            {key:value + 1 for key,value in before_scopes.items()})
        row = records()['enrollment']['E-A-1']
        scopes = academic_coverage()
        scopes[0] = coverage('enrollment', ['E-A-2','E-A-3','E-A-4','E-A-5'])
        scopes[1] = coverage('final_result', ['R-A-2','R-A-3','R-A-4'])
        scopes[3] = coverage('assessment', [])
        guard = AuthorizationGuard.objects.get().revision
        self.publish(self.followup([change('enrollment', row, self.source, 'EV-WITHDRAW', status='withdrawn')],
            academic_coverage_value=scopes))
        self.assertEqual(Enrollment.objects.get(source_id='E-A-1').status, 'withdrawn')
        self.assertTrue(FinalResult.objects.filter(source_id='R-A-1').exists())
        self.assertEqual(StudentProfile.objects.get(source_id='SP-A').scope_revision, before_scopes['SP-A'] + 2)
        self.assertEqual(StudentProfile.objects.get(source_id='SP-B').scope_revision, before_scopes['SP-B'] + 1)
        self.assertEqual(AuthorizationGuard.objects.get().revision, guard + 1)
        self.assertEqual(self.protected(), protected)

    def test_database_constraints_and_all_academic_history_are_immutable(self):
        self.initial()
        for model in [EnrollmentChange, FinalResultChange, RequirementChange, AssessmentChange, AcademicCoverage]:
            target = model.objects.first()
            with self.subTest(model=model.__name__), self.assertRaises(IntegrityError), transaction.atomic():
                model.objects.filter(pk=target.pk).update(id=target.pk)
        checks = [(FinalResult, dict(grade=0)), (FinalResult, dict(pass_status='passed')),
            (OutstandingRequirement, dict(status='closed')), (OutstandingRequirement, dict(reason='')),
            (Enrollment, dict(status='unknown')), (AssessmentEvent, dict(status='unknown'))]
        for model, values in checks:
            with self.subTest(model=model.__name__, values=values), self.assertRaises(IntegrityError), transaction.atomic():
                model.objects.update(**values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(schedule_change=self.old_notification.schedule_change,
                group=self.old_notification.group, final_result_change=FinalResultChange.objects.first(), published_at=timezone.now())
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(published_at=timezone.now())

    def test_schema4_cannot_adopt_unimported_academic_state(self):
        Enrollment.objects.create(dataset_id=DATASET_ID, source_id='UNIMPORTED', record_revision=1,
            source_updated_at=self.source, student=StudentProfile.objects.get(source_id='SP-A'),
            course=Course.objects.get(source_id='C-S1'), period=Period.objects.get(source_id=PERIOD), status='active')
        with self.assertRaisesRegex(ImportRejected, 'пусты'):
            self.publish(build_payload(self.source))

    def test_replay_conflict_stale_parent_downgrade_and_unsupported_changes(self):
        accepted = self.initial()
        before = canonical_state()
        conflicting = deepcopy(accepted)
        conflicting['preparation_reason'] = 'Другое содержимое принятого номера'
        with self.assertRaises(ImportRejected):
            apply(signed(conflicting), expected_revision=5, operator='s4-test')
        with self.assertRaises(ImportRejected):
            apply(signed(self.followup([])), expected_revision=4, operator='s4-test')
        downgrade = self.followup([])
        downgrade.update(schema_version=3, subset='s3_planner_v3')
        del downgrade['academic_coverage']
        self.assert_rejected_unchanged(downgrade)
        row = records()['enrollment']['E-A-1']
        self.assert_rejected_unchanged(self.followup([change('enrollment', row, self.source, 'EV-REBIND', student_id='SP-B')]))
        row = records()['requirement']['Q-A-4']
        self.assert_rejected_unchanged(self.followup([change('requirement', row, self.source, 'EV-IMPLICIT-CLOSE',
            status='closed', closed_at=self.source)]))
        self.assertEqual(canonical_state(), before)
