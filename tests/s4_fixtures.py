"""Изолированные кандидаты S4; ожидаемые числа задаются отдельно в тестах."""
from copy import deepcopy
import json
from pathlib import Path

from data_import.contracts import content_hash, validate


ROOT = Path(__file__).resolve().parents[1]
PERIOD = 'P-2026-autumn'


def signed(payload):
    payload['content_sha256'] = content_hash(payload)
    return validate(payload)


def create(kind, source, source_id, **values):
    operation = dict(operation='create', entity_type=kind,
        record=dict(source_id=source_id, record_revision=1, source_updated_at=source, **values))
    if kind in {'enrollment', 'final_result', 'requirement', 'assessment', 'announcement'}:
        operation['event_id'] = 'EV-' + source_id
    return operation


def change(kind, before, source, event_id, change_type='correction', **changed):
    return dict(operation='change', entity_type=kind, event_id=event_id,
        expected_record_revision=before['record_revision'], change_type=change_type,
        before=deepcopy(before),
        after=dict(before, record_revision=before['record_revision'] + 1, source_updated_at=source, **changed),
        reason='Изолированная проверка изменения учебной записи')


def coverage(entity_type, ids=None, student='SP-A', completeness='complete', period=PERIOD):
    ids = [] if ids is None else ids
    return dict(entity_type=entity_type, student_id=student, period_id=period, completeness=completeness,
        record_ids=list(ids) if completeness == 'complete' else None,
        record_count=len(ids) if completeness == 'complete' else None)


def academic_coverage():
    return [
        coverage('enrollment', ['E-A-1','E-A-2','E-A-3','E-A-4','E-A-5']),
        coverage('final_result', ['R-A-1','R-A-2','R-A-3','R-A-4']),
        coverage('requirement', ['Q-A-4']), coverage('assessment', ['AS-A-1']),
        coverage('enrollment', ['E-B-1','E-B-2','E-B-3','E-B-4'], student='SP-B'),
        coverage('final_result', ['R-B-1','R-B-2'], student='SP-B'),
        coverage('requirement', [], student='SP-B'), coverage('assessment', ['AS-B-1'], student='SP-B'),
    ]


def academic_operations(source):
    operations = [create('course', source, f'C-T-{index}', name=f'Учебная дисциплина {index} (демо)',
        period_id=PERIOD) for index in range(1, 6)]
    for student, count in [('A', 5), ('B', 4)]:
        for index in range(1, count + 1):
            operations.append(create('enrollment', source, f'E-{student}-{index}', student_id=f'SP-{student}',
                course_id=f'C-T-{index}', period_id=PERIOD, status='active'))
    for student, values in [('A', [5, 4, 'passed', 2]), ('B', [4, 'passed'])]:
        for index, value in enumerate(values, 1):
            numeric = type(value) is int
            operations.append(create('final_result', source, f'R-{student}-{index}',
                enrollment_id=f'E-{student}-{index}', result_kind='grade' if numeric else 'pass_fail',
                grade=value if numeric else None, pass_status=None if numeric else value,
                recorded_at=source))
    operations.append(create('requirement', source, 'Q-A-4', enrollment_id='E-A-4', result_id='R-A-4',
        reason='Неудовлетворительный итог: требуется повторная аттестация (демо).', status='open',
        opened_at=source, due_at='2026-11-10T18:00:00+03:00', closed_at=None))
    for student, group, date in [('A', 'G-MB21', '2026-11-09'), ('B', 'G-EK11', '2026-11-10')]:
        operations.append(create('assessment', source, f'AS-{student}-1', group_id=group, course_id='C-T-1',
            period_id=PERIOD, assessment_kind='exam', instructor_id='I-S1', instructor_status='provided',
            starts_at=f'{date}T14:00:00+03:00', ends_at=f'{date}T15:30:00+03:00', status='active',
            room='215', location_status='provided'))
    return operations


def build_payload(source, operations=None, revision=5, academic_coverage_value=None, schedule_coverage=None):
    payload = json.loads((ROOT / 'fixtures/s2/r4.json').read_text(encoding='utf-8'))
    payload.update(schema_version=4, subset='s4_studies_v4', dataset_revision=revision,
        parent_revision=revision - 1, source_as_of=source,
        prepared_by='Изолированные проверки S4', preparation_reason='Кандидат только для disposable PostgreSQL tests',
        operations=academic_operations(source) if operations is None else operations,
        academic_coverage=academic_coverage() if academic_coverage_value is None else academic_coverage_value)
    if schedule_coverage is not None:
        payload['coverage'] = deepcopy(schedule_coverage)
    return payload
