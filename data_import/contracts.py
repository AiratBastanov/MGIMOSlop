"""Строгая схема S1: явные поля и операции, без доступа к БД."""
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

DATASET_ID = 's1-schedule'
MOSCOW = ZoneInfo('Europe/Moscow')
BASE_FIELDS = {'source_id','record_revision','source_updated_at'}
RECORD_FIELDS = {
    'period': BASE_FIELDS | {'name','date_from','date_until'},
    'group': BASE_FIELDS | {'name'},
    'course': BASE_FIELDS | {'name','period_id'},
    'instructor': BASE_FIELDS | {'name'},
    'lesson': BASE_FIELDS | {'group_id','course_id','period_id','instructor_id',
                            'instructor_status','starts_at','ends_at','status','room','location_status'},
}
S2_RECORD_FIELDS = {**RECORD_FIELDS,
    'faculty': BASE_FIELDS | {'name'},
    'programme': BASE_FIELDS | {'name','faculty_id'},
    'group': BASE_FIELDS | {'name','programme_id'},
    'profile': BASE_FIELDS | {'name','group_id','year'},
}
S3_RECORD_FIELDS = {**S2_RECORD_FIELDS,
    'announcement': BASE_FIELDS | {'title','body','audience','faculty_id','group_id','student_id',
                                  'published_at','visible_until','due_at','status'},
}
S4_RECORD_FIELDS = {**S3_RECORD_FIELDS,
    'enrollment': BASE_FIELDS | {'student_id', 'course_id', 'period_id', 'status'},
    'final_result': BASE_FIELDS | {'enrollment_id', 'result_kind', 'grade', 'pass_status', 'recorded_at'},
    'requirement': BASE_FIELDS | {'enrollment_id', 'result_id', 'reason', 'status', 'opened_at', 'due_at', 'closed_at'},
    'assessment': RECORD_FIELDS['lesson'] | {'assessment_kind'},
}
ACADEMIC_KINDS = {'enrollment', 'final_result', 'requirement', 'assessment'}

def fields_for(schema_version):
    return {1:RECORD_FIELDS, 2:S2_RECORD_FIELDS, 3:S3_RECORD_FIELDS, 4:S4_RECORD_FIELDS}[schema_version]

class ImportRejected(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)

class SourceUnavailable(ImportRejected):
    pass

def reject(code, message):
    raise ImportRejected(code, message)

def fields(value, expected, label):
    if type(value) is not dict or set(value) != expected:
        reject('structure', f'{label}: требуется точный набор полей {sorted(expected)}')

def string(value, label, limit=100):
    if type(value) is not str or not value.strip() or len(value) > limit or '\x00' in value:
        reject('structure', f'{label}: требуется непустая строка длиной до {limit}')
    return value

def identity(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', value):
        reject('identity', 'Некорректный стабильный source_id/event_id')
    return value

def integer(value, label, minimum=1):
    if type(value) is not int or value < minimum:
        reject('structure', f'{label}: требуется целое число >= {minimum}')
    return value

def day(value):
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        reject('date', 'Ожидается дата YYYY-MM-DD')
    try:
        return date.fromisoformat(value)
    except ValueError:
        reject('date', 'Несуществующая дата')

def instant(value):
    if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', value):
        reject('timestamp', 'Требуется ISO datetime с секундами и явным offset')
    try:
        result = datetime.fromisoformat(value).astimezone(timezone.utc)
        # И хранение в UTC, и обязательное отображение в Moscow должны быть
        # представимы. Offset на границе годов 0001/9999 может переполнить date.
        result.astimezone(MOSCOW)
        return result
    except (ValueError, OverflowError):
        reject('timestamp', 'Некорректный или непредставимый datetime')

def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')

def content_hash(payload):
    return hashlib.sha256(canonical_bytes({k:v for k,v in payload.items() if k != 'content_sha256'})).hexdigest()

def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

def record(kind, value, source_as_of, schema_version=1):
    allowed = fields_for(schema_version)
    if type(kind) is not str or kind not in allowed:
        reject('unsupported_entity', 'Сущность не входит в S1')
    fields(value, allowed[kind], kind)
    result = value.copy()
    identity(value['source_id'])
    integer(value['record_revision'], 'record_revision')
    updated = instant(value['source_updated_at'])
    if updated > source_as_of:
        reject('source_time', 'source_updated_at позже source_as_of')
    result['source_updated_at'] = updated.isoformat()
    if kind not in {'lesson', 'announcement'} | ACADEMIC_KINDS:
        string(value['name'], 'name', 120 if kind == 'course' else 100)
    if kind == 'period':
        if day(value['date_from']) >= day(value['date_until']):
            reject('interval', 'Пустой или обратный учебный период')
    if kind == 'course':
        identity(value['period_id'])
    if schema_version >= 2:
        for field in {'programme':'faculty_id','group':'programme_id','profile':'group_id'}.get(kind, '').split():
            identity(value[field])
        if kind == 'profile' and integer(value['year'],'year') > 6:
            reject('year','Курс должен быть от 1 до 6')
    if kind == 'announcement':
        string(value['title'],'title',180)
        string(value['body'],'body',4000)
        targets = {'all_students':None,'faculty':'faculty_id','group':'group_id','student':'student_id'}
        if type(value['audience']) is not str or value['audience'] not in targets:
            reject('audience','Неизвестная аудитория объявления')
        for field in ['faculty_id','group_id','student_id']:
            if field == targets[value['audience']]:
                identity(value[field])
            elif value[field] is not None:
                reject('audience','Аудитория требует ровно соответствующий target')
        published = instant(value['published_at'])
        result['published_at'] = published.isoformat()
        for field in ['visible_until','due_at']:
            if value[field] is not None:
                moment = instant(value[field])
                if moment < published or (field == 'visible_until' and moment == published):
                    reject('interval','Окончание видимости/срок объявления не согласованы с публикацией')
                result[field] = moment.isoformat()
        if value['status'] not in ['draft','published','withdrawn']:
            reject('enum','Неизвестный статус объявления')
    if kind in ['lesson', 'assessment']:
        for key in ['group_id','course_id','period_id']:
            identity(value[key])
        for key, status in [('instructor_id','instructor_status'),('room','location_status')]:
            if value[status] == 'provided':
                (identity(value[key]) if key == 'instructor_id' else string(value[key],key,60))
            elif value[status] != 'not_provided' or value[key] is not None:
                reject('missing_field_status', 'Null и статус отсутствующего поля не согласованы')
        start, end = instant(value['starts_at']), instant(value['ends_at'])
        if end <= start or start.astimezone(MOSCOW).date() != end.astimezone(MOSCOW).date():
            reject('interval', 'Занятие должно закончиться позже начала в тот же день МСК')
        if value['status'] not in ['active','cancelled']:
            reject('enum', 'Неизвестный статус занятия')
        result.update(starts_at=start.isoformat(), ends_at=end.isoformat())
    if kind in ACADEMIC_KINDS:
        validate_academic_record(kind, value, result, updated)
    return result


def validate_academic_record(kind, value, result, updated):
    if kind == 'enrollment':
        for key in ['student_id', 'course_id', 'period_id']:
            identity(value[key])
        if value['status'] not in ['active', 'withdrawn']:
            reject('enum', 'Неизвестный статус назначения дисциплины')
    elif kind == 'final_result':
        identity(value['enrollment_id'])
        if value['result_kind'] == 'grade':
            if type(value['grade']) is not int or not 2 <= value['grade'] <= 5 or value['pass_status'] is not None:
                reject('result_value', 'Оценка должна быть целым числом 2–5 без pass_status')
        elif value['result_kind'] == 'pass_fail':
            if value['grade'] is not None or value['pass_status'] not in ['passed', 'failed']:
                reject('result_value', 'Зачёт требует passed/failed без числовой оценки')
        else:
            reject('result_value', 'Неизвестная шкала результата')
        recorded = instant(value['recorded_at'])
        if recorded > updated:
            reject('source_time', 'Время результата позже его исходного обновления')
        result['recorded_at'] = recorded.isoformat()
    elif kind == 'requirement':
        identity(value['enrollment_id'])
        if value['result_id'] is not None:
            identity(value['result_id'])
        string(value['reason'], 'reason', 500)
        opened = instant(value['opened_at'])
        if opened > updated:
            reject('source_time', 'Требование открыто позже исходного обновления')
        result['opened_at'] = opened.isoformat()
        due = instant(value['due_at']) if value['due_at'] is not None else None
        closed = instant(value['closed_at']) if value['closed_at'] is not None else None
        if due is not None and due < opened:
            reject('interval', 'Срок требования раньше открытия')
        if value['status'] == 'open':
            if closed is not None:
                reject('requirement_state', 'Открытое требование не имеет closed_at')
        elif value['status'] == 'closed':
            if closed is None or not opened <= closed <= updated:
                reject('requirement_state', 'Закрытие требует время между opened_at и source_updated_at')
        else:
            reject('enum', 'Неизвестный статус требования')
        result['due_at'] = due.isoformat() if due is not None else None
        result['closed_at'] = closed.isoformat() if closed is not None else None
    elif value['assessment_kind'] not in ['exam', 'pass_fail']:
        reject('enum', 'Неизвестный вид аттестации')

@dataclass(frozen=True)
class Operation:
    kind: str
    action: str
    after: dict[str, Any]
    before: dict[str, Any] | None = None
    event_id: str | None = None
    change_type: str | None = None
    reason: str | None = None
    expected_record_revision: int | None = None

@dataclass(frozen=True)
class CoverageSpec:
    group_id: str
    period_id: str
    date_from: date
    date_until: date
    completeness: str
    active_ids: tuple[str, ...] | None
    active_count: int | None


@dataclass(frozen=True)
class AcademicCoverageSpec:
    student_id: str
    period_id: str
    entity_type: str
    completeness: str
    record_ids: tuple[str, ...] | None
    record_count: int | None

@dataclass(frozen=True)
class DatasetBatch:
    payload: dict[str, Any]
    revision: int
    parent: int | None
    source_as_of: datetime
    valid_from: datetime
    valid_until: datetime
    operations: tuple[Operation, ...]
    coverage: tuple[CoverageSpec, ...]
    academic_coverage: tuple[AcademicCoverageSpec, ...] = ()

def validate(payload):
    schema = payload.get('schema_version') if type(payload) is dict else None
    fields(payload, {'schema_version','subset','dataset_id','dataset_revision','parent_revision',
                     'content_sha256','provenance','prepared_by','preparation_reason','source_as_of',
                     'valid_from','valid_until','operations','coverage'} | ({'academic_coverage'} if schema == 4 else set()), 'manifest')
    if type(schema) is not int or (schema,payload['subset']) not in [(1,'s1_schedule_v1'),(2,'s2_profiles_v2'),(3,'s3_planner_v3'),(4,'s4_studies_v4')]:
        reject('schema', 'Неподдерживаемая версия схемы/среза')
    if payload['dataset_id'] != DATASET_ID or payload['provenance'] != 'synthetic':
        reject('provenance', 'Поддерживается только синтетический набор s1-schedule')
    revision = integer(payload['dataset_revision'], 'dataset_revision')
    parent = payload['parent_revision']
    if (revision == 1 and parent is not None) or (revision > 1 and integer(parent,'parent_revision') != revision - 1):
        reject('parent', 'Ревизии последовательны; первая не имеет родителя')
    string(payload['prepared_by'],'prepared_by')
    string(payload['preparation_reason'],'preparation_reason',500)
    source_as_of = instant(payload['source_as_of'])
    valid_from, valid_until = instant(payload['valid_from']), instant(payload['valid_until'])
    if valid_until <= valid_from:
        reject('interval', 'Обратный или пустой valid interval')
    if type(payload['content_sha256']) is not str or not re.fullmatch('[a-f0-9]{64}',payload['content_sha256']) or content_hash(payload) != payload['content_sha256']:
        reject('checksum', 'SHA256 содержимого не совпадает')
    if type(payload['operations']) is not list or len(payload['operations']) > 5000 or type(payload['coverage']) is not list or len(payload['coverage']) > 5000:
        reject('structure', 'operations/coverage должны быть списками не длиннее 5000')
    operations, seen, events = [], set(), set()
    for raw in payload['operations']:
        if type(raw) is not dict:
            reject('structure','Операция должна быть объектом')
        action, kind = raw.get('operation'), raw.get('entity_type')
        if type(action) is not str or type(kind) is not str:
            reject('structure', 'operation/entity_type должны быть строками')
        if action == 'create':
            event_create = (kind == 'announcement' and schema >= 3) or (kind in ACADEMIC_KINDS and schema == 4)
            fields(raw, {'operation','entity_type','record'} | ({'event_id'} if event_create else set()), 'create')
            after = record(kind, raw['record'], source_as_of, schema)
            if after['record_revision'] != 1:
                reject('record_revision','Создание начинается с record_revision=1')
            if event_create:
                if kind == 'announcement' and after['status'] == 'withdrawn':
                    reject('operation','Новое объявление начинается с draft или published')
                event_id = identity(raw['event_id'])
                if kind == 'enrollment' and after['status'] != 'active':
                    reject('operation', 'Новое назначение дисциплины должно быть active')
                op = Operation(kind,action,after,event_id=event_id,change_type='create',reason='Первичная подготовка объявления' if kind == 'announcement' else 'Первичная подготовка учебной записи')
            else:
                op = Operation(kind,action,after)
        elif action == 'change' and (kind == 'lesson' or (kind == 'profile' and schema >= 2) or (kind == 'announcement' and schema >= 3) or (kind in ACADEMIC_KINDS and schema == 4)):
            fields(raw, {'operation','entity_type','expected_record_revision','event_id','change_type','before','after','reason'},'change')
            before, after = record(kind,raw['before'],source_as_of,schema), record(kind,raw['after'],source_as_of,schema)
            expected = integer(raw['expected_record_revision'],'expected_record_revision')
            if before['record_revision'] != expected or after['record_revision'] != expected + 1:
                reject('record_revision','Изменение должно продвигать record_revision ровно на один')
            stable = {'profile':['source_id','name','year'],
                      'lesson':['source_id','group_id','course_id','period_id'],
                      'announcement':['source_id','audience','faculty_id','group_id','student_id'],
                      'enrollment':['source_id','student_id','course_id','period_id'],
                      'final_result':['source_id','enrollment_id'],
                      'requirement':['source_id','enrollment_id','opened_at'],
                      'assessment':['source_id','group_id','course_id','period_id']}[kind]
            for key in stable:
                if before[key] != after[key]:
                    reject('access_change_unsupported', 'S1 не импортирует изменение identity/учебной области доступа')
            if instant(after['source_updated_at']) < instant(before['source_updated_at']):
                reject('source_time','Время обновления не может двигаться назад')
            change_types = {'announcement':['correction','publish','withdraw'],
                            'enrollment':['correction'], 'final_result':['correction'],
                            'requirement':['correction','close','reopen']}.get(kind, ['correction','move','cancel','substitute'])
            if raw['change_type'] not in change_types:
                reject('operation','Неизвестный тип изменения')
            changed = {key for key in after if before[key] != after[key]} - {'record_revision','source_updated_at'}
            if kind == 'profile' and (changed != {'group_id'} or raw['change_type'] != 'correction'):
                reject('unsupported_operation','S2 разрешает только явную коррекцию группы профиля')
            if kind == 'announcement':
                transition = (before['status'], after['status'])
                if before['status'] == 'withdrawn':
                    reject('operation','Отозванное объявление не изменяется и не публикуется повторно')
                if raw['change_type'] == 'publish' and transition != ('draft','published'):
                    reject('operation','publish требует переход draft → published')
                if raw['change_type'] == 'withdraw' and (transition != ('published','withdrawn') or changed != {'status'}):
                    reject('operation','withdraw меняет только published → withdrawn')
                if raw['change_type'] == 'correction' and before['status'] != after['status']:
                    reject('operation','correction сохраняет статус объявления')
                if before['status'] == 'published' and before['published_at'] != after['published_at']:
                    reject('operation','Дата исходной публикации неизменна после published')
            if kind == 'enrollment' and changed != {'status'}:
                reject('unsupported_operation', 'Назначение меняет только свой явный status')
            if kind == 'requirement':
                if raw['change_type'] == 'correction' and changed & {'status', 'closed_at'}:
                    reject('operation', 'Закрытие/повторное открытие требования — отдельная явная операция')
                if raw['change_type'] == 'close' and ((before['status'], after['status']) != ('open', 'closed') or changed != {'status', 'closed_at'}):
                    reject('operation', 'close меняет только open → closed и closed_at')
                if raw['change_type'] == 'reopen' and ((before['status'], after['status']) != ('closed', 'open') or changed != {'status', 'closed_at'}):
                    reject('operation', 'reopen меняет только closed → open и closed_at')
            required = {'move': {'starts_at','ends_at'}, 'cancel': {'status'}, 'substitute': {'instructor_id'}}
            if not changed or (raw['change_type'] in required and not changed & required[raw['change_type']]):
                reject('operation','Тип изменения не соответствует изменённым полям')
            if raw['change_type'] == 'cancel' and after['status'] != 'cancelled':
                reject('operation','cancel требует cancelled')
            event_id = identity(raw['event_id'])
            op = Operation(kind,action,after,before,event_id,raw['change_type'],string(raw['reason'],'reason',500),expected)
        else:
            reject('unsupported_operation','Операция/сущность вне явно поддерживаемого S1')
        key = (kind,after['source_id'])
        if op.event_id is not None:
            if op.event_id in events:
                reject('duplicate','Повтор event_id')
            events.add(op.event_id)
        if key in seen:
            reject('duplicate','Повтор source_id в одном типе сущности')
        seen.add(key)
        operations.append(op)
    coverages = []
    for raw in payload['coverage']:
        fields(raw, {'entity_type','group_id','period_id','date_from','date_until','completeness','active_ids','active_count'},'coverage')
        if raw['entity_type'] != 'lesson':
            reject('unsupported_entity','S1 coverage только для lesson')
        start, end = day(raw['date_from']), day(raw['date_until'])
        if end <= start:
            reject('interval','Пустой coverage')
        completeness = raw['completeness']
        ids, count = raw['active_ids'], raw['active_count']
        if completeness == 'complete':
            if type(ids) is not list or len(ids) != integer(count,'active_count',0):
                reject('coverage','active_ids/count не согласованы')
            for source_id in ids:
                identity(source_id)
            if len(set(ids)) != len(ids):
                reject('duplicate','Повтор ID в coverage')
        elif completeness not in ['partial','unknown'] or ids is not None or count is not None:
            reject('coverage','partial/unknown не объявляет точный список/count')
        spec = CoverageSpec(identity(raw['group_id']),identity(raw['period_id']),start,end,completeness,tuple(sorted(ids)) if ids is not None else None,count)
        for old in coverages:
            if (old.group_id,old.period_id) == (spec.group_id,spec.period_id) and old.date_from < end and start < old.date_until:
                reject('coverage','Пересекающиеся области coverage')
        coverages.append(spec)
    academic_coverages = []
    if schema == 4:
        if type(payload['academic_coverage']) is not list or len(payload['academic_coverage']) > 5000:
            reject('structure', 'academic_coverage должен быть списком не длиннее 5000')
        scopes = set()
        for raw in payload['academic_coverage']:
            fields(raw, {'entity_type','student_id','period_id','completeness','record_ids','record_count'}, 'academic_coverage')
            if type(raw['entity_type']) is not str or raw['entity_type'] not in ACADEMIC_KINDS:
                reject('coverage', 'Неизвестный домен учебной полноты')
            student, period = identity(raw['student_id']), identity(raw['period_id'])
            scope = (student, period, raw['entity_type'])
            if scope in scopes:
                reject('duplicate', 'Повтор области academic_coverage')
            scopes.add(scope)
            completeness, ids, count = raw['completeness'], raw['record_ids'], raw['record_count']
            if completeness == 'complete':
                if type(ids) is not list or len(ids) != integer(count, 'record_count', 0):
                    reject('coverage', 'record_ids/count не согласованы')
                for source_id in ids:
                    identity(source_id)
                if len(set(ids)) != len(ids):
                    reject('duplicate', 'Повтор ID в academic_coverage')
            elif completeness not in ['partial', 'unknown'] or ids is not None or count is not None:
                reject('coverage', 'partial/unknown не объявляет точный список/count')
            academic_coverages.append(AcademicCoverageSpec(student, period, raw['entity_type'], completeness,
                tuple(sorted(ids)) if ids is not None else None, count))
    return DatasetBatch(payload,revision,parent,source_as_of,valid_from,valid_until,tuple(operations),tuple(coverages),tuple(academic_coverages))
