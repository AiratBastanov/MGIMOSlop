from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, time
from django.db import connection, transaction
from django.utils import timezone
from .contracts import ACADEMIC_KINDS, DATASET_ID, MOSCOW, validate, integer, string, instant, day, reject
from .models import AcademicCoverage, Dataset, ImportRevision, Coverage
from .state import ACADEMIC_HISTORY, MODELS, records, state_hash
from studies.models import Lesson, ScheduleChange, ProfileChange, StudentProfile
from notifications.services import record_schedule_change
from notifications.models import Announcement, AnnouncementChange, Notification


def record_announcement_change(change, *, published_at):
    """Required write in the same transaction; never replaces an existing event."""
    return Notification.objects.create(announcement_change=change,published_at=published_at)


def record_final_result_change(change, *, published_at):
    return Notification.objects.create(final_result_change=change, published_at=published_at)


def record_requirement_change(change, *, published_at):
    return Notification.objects.create(requirement_change=change, published_at=published_at)


def record_assessment_change(change, *, published_at):
    return Notification.objects.create(assessment_change=change, published_at=published_at)

@contextmanager
def read_snapshot():
    if connection.in_atomic_block:
        raise RuntimeError('Снимок требует отдельной внешней транзакции')
    with transaction.atomic(durable=True):
        with connection.cursor() as cursor:
            cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        yield

def initialize():
    with transaction.atomic(durable=True):
        from accounts.models import AuthorizationGuard
        AuthorizationGuard.objects.get_or_create(pk=1)
        head, created = Dataset.objects.get_or_create(source_id=DATASET_ID)
        head = Dataset.objects.select_for_update().get(pk=DATASET_ID)
        if created:
            head.state_hash = state_hash()
            head.save(update_fields=['state_hash'])
        else:
            check_integrity(head)
        return {'created':created,'revision':head.current_revision}

def check_integrity(head):
    from .state import current_schema
    from studies.models import Faculty, Programme, Group
    if current_schema() == 1 and (Faculty.objects.exists() or Programme.objects.exists() or
            StudentProfile.objects.exists() or ProfileChange.objects.exists() or Group.objects.filter(programme__isnull=False).exists()):
        reject('integrity','До принятия schema 2 новые учебные области должны быть пусты')
    if current_schema() < 3 and (Announcement.objects.exists() or AnnouncementChange.objects.exists() or
            Notification.objects.filter(announcement_change__isnull=False).exists()):
        reject('integrity','До принятия schema 3 новые области объявлений должны быть пусты')
    if current_schema() < 4 and (any(MODELS[kind].objects.exists() for kind in ACADEMIC_KINDS) or
            any(model.objects.exists() for model, _ in ACADEMIC_HISTORY.values()) or AcademicCoverage.objects.exists() or
            Notification.objects.filter(final_result_change__isnull=False).exists() or
            Notification.objects.filter(requirement_change__isnull=False).exists() or
            Notification.objects.filter(assessment_change__isnull=False).exists()):
        reject('integrity', 'До принятия schema 4 новые учебные области должны быть пусты')
    revisions = list(ImportRevision.objects.filter(dataset=head).order_by('revision').values_list('revision',flat=True))
    if revisions != list(range(1,head.current_revision + 1)) or head.state_hash != state_hash():
        reject('integrity', 'Source-owned состояние отличается от принятого отпечатка; импорт остановлен')

def check_head(head, batch, expected):
    integer(expected,'expected_revision',0)
    if expected != head.current_revision:
        reject('head_conflict', f'Ожидалась revision={expected}, принята revision={head.current_revision}')
    if batch.revision == head.current_revision:
        accepted = ImportRevision.objects.get(dataset=head,revision=head.current_revision)
        if accepted.content_hash != batch.payload['content_sha256']:
            reject('revision_content_conflict', 'Номер принятой revision нельзя использовать с другим содержимым')
        return 'noop'
    if batch.revision < head.current_revision:
        reject('older_revision','Откат к старой revision запрещён')
    if (batch.parent or 0) != head.current_revision or batch.revision != head.current_revision + 1:
        reject('stale_parent','Parent не совпадает с текущей принятой revision')
    return 'apply'

def proposed_state(batch, current):
    result = deepcopy(current)
    diff = []
    for op in batch.operations:
        source_id = op.after['source_id']
        existing = result[op.kind].get(source_id)
        if op.action == 'create' and existing is not None:
            reject('existing_id',f'Запись {source_id} уже существует; create не перезаписывает её')
        if op.event_id is not None and (ScheduleChange.objects.filter(dataset_id=DATASET_ID,event_id=op.event_id).exists() or
                ProfileChange.objects.filter(dataset_id=DATASET_ID,event_id=op.event_id).exists() or
                AnnouncementChange.objects.filter(dataset_id=DATASET_ID,event_id=op.event_id).exists() or
                any(model.objects.filter(dataset_id=DATASET_ID,event_id=op.event_id).exists() for model, _ in ACADEMIC_HISTORY.values())):
            reject('event_conflict','event_id уже принят')
        if op.action == 'change':
            if existing != op.before:
                reject('before_conflict',f'Before/record_revision {source_id} не совпадает с БД')
        result[op.kind][source_id] = op.after
        diff.append({'entity_type':op.kind,'source_id':source_id,'operation':op.action,
                     'before':existing,'after':op.after})
    validate_result(batch,result)
    return result, diff

def validate_result(batch, result):
    if batch.payload['schema_version'] >= 2:
        for programme in result['programme'].values():
            if programme['faculty_id'] not in result['faculty']:
                reject('reference','Неизвестный факультет программы')
        for group in result['group'].values():
            # Сохранённая ДЕМО-21 из S1 остаётся без программы и не получает профиль.
            if group['programme_id'] is not None and group['programme_id'] not in result['programme']:
                reject('reference','Неизвестная программа группы')
        for profile in result['profile'].values():
            group = result['group'].get(profile['group_id'])
            if not group or not group['programme_id']:
                reject('reference','Профиль требует группу с программой и факультетом')
        for op in batch.operations:
            if op.kind == 'profile' and op.action == 'change':
                old = result['group'][op.before['group_id']]
                new = result['group'][op.after['group_id']]
                if old['programme_id'] != new['programme_id']:
                    reject('scope','S2 коррекция группы разрешена только внутри одной программы')
    if batch.payload['schema_version'] >= 3:
        for announcement in result['announcement'].values():
            for field,kind in [('faculty_id','faculty'),('group_id','group'),('student_id','profile')]:
                if announcement[field] is not None and announcement[field] not in result[kind]:
                    reject('reference',f'Объявление ссылается на неизвестный {field}')
    for course in result['course'].values():
        if course['period_id'] not in result['period']:
            reject('reference','Course ссылается на неизвестный Period')
    if batch.payload['schema_version'] >= 4:
        validate_academic_result(batch, result)
    lessons = list(result['lesson'].values())
    for lesson in lessons:
        for field,kind in [('group_id','group'),('course_id','course'),('period_id','period'),('instructor_id','instructor')]:
            if lesson[field] is not None and lesson[field] not in result[kind]:
                reject('reference',f'{lesson["source_id"]}: неизвестная ссылка {field}')
        if result['course'][lesson['course_id']]['period_id'] != lesson['period_id']:
            reject('reference','Period занятия и дисциплины различаются')
        period = result['period'][lesson['period_id']]
        date = instant(lesson['starts_at']).astimezone(MOSCOW).date()
        if not day(period['date_from']) <= date < day(period['date_until']):
            reject('interval','Занятие вне учебного периода')
        if not batch.valid_from <= instant(lesson['starts_at']) < instant(lesson['ends_at']) <= batch.valid_until:
            reject('interval','Занятие вне valid interval набора')
    active = [x for x in lessons if x['status'] == 'active']
    for i, left in enumerate(active):
        for right in active[i+1:]:
            same_scope = left['group_id'] == right['group_id'] or (left['instructor_id'] is not None and left['instructor_id'] == right['instructor_id'])
            if same_scope and instant(left['starts_at']) < instant(right['ends_at']) and instant(right['starts_at']) < instant(left['ends_at']):
                reject('overlap','Пересечение активных занятий группы или преподавателя')
    for spec in batch.coverage:
        if spec.group_id not in result['group'] or spec.period_id not in result['period']:
            reject('reference','Coverage ссылается на неизвестную группу/период')
        start = datetime.combine(spec.date_from,time.min,MOSCOW)
        end = datetime.combine(spec.date_until,time.min,MOSCOW)
        if not batch.valid_from <= start < end <= batch.valid_until:
            reject('coverage','Coverage вне valid interval')
        period = result['period'][spec.period_id]
        if not day(period['date_from']) <= spec.date_from < spec.date_until <= day(period['date_until']):
            reject('coverage','Coverage вне учебного периода')
        actual = sorted(x['source_id'] for x in active if x['group_id'] == spec.group_id and x['period_id'] == spec.period_id and spec.date_from <= instant(x['starts_at']).astimezone(MOSCOW).date() < spec.date_until)
        if spec.completeness == 'complete' and (actual != list(spec.active_ids) or len(actual) != spec.active_count):
            reject('coverage','Complete coverage не совпадает с итоговыми активными IDs/count, включая сохранённые записи')


def validate_academic_result(batch, result):
    """Проверяет весь proposed state: и операции пакета, и сохранённые записи."""
    enrollments = result['enrollment']
    assignments = set()
    for enrollment in enrollments.values():
        for field, kind in [('student_id','profile'), ('course_id','course'), ('period_id','period')]:
            if enrollment[field] not in result[kind]:
                reject('reference', f'Назначение ссылается на неизвестный {field}')
        if result['course'][enrollment['course_id']]['period_id'] != enrollment['period_id']:
            reject('reference', 'Период назначения и дисциплины различаются')
        key = (enrollment['student_id'], enrollment['course_id'], enrollment['period_id'])
        if key in assignments:
            reject('duplicate', 'Повтор назначения student/course/period')
        assignments.add(key)
    result_enrollments = set()
    for final in result['final_result'].values():
        if final['enrollment_id'] not in enrollments:
            reject('reference', 'Результат ссылается на неизвестное назначение')
        if final['enrollment_id'] in result_enrollments:
            reject('duplicate', 'Назначение имеет более одного итогового результата')
        result_enrollments.add(final['enrollment_id'])
    requirements = list(result['requirement'].values())
    for requirement in requirements:
        if requirement['enrollment_id'] not in enrollments:
            reject('reference', 'Требование ссылается на неизвестное назначение')
        if requirement['result_id'] is not None:
            final = result['final_result'].get(requirement['result_id'])
            if final is None or final['enrollment_id'] != requirement['enrollment_id']:
                reject('ownership', 'Связанный результат требования должен принадлежать тому же назначению')
    for final in result['final_result'].values():
        negative = final['grade'] == 2 if final['result_kind'] == 'grade' else final['pass_status'] == 'failed'
        if negative and not any(requirement['status'] == 'open' and requirement['result_id'] == final['source_id']
                and requirement['enrollment_id'] == final['enrollment_id'] for requirement in requirements):
            reject('result_requirement', 'Неудовлетворительный итог требует соответствующее открытое требование')
    assessments = list(result['assessment'].values())
    for assessment in assessments:
        for field, kind in [('group_id','group'),('course_id','course'),('period_id','period'),('instructor_id','instructor')]:
            if assessment[field] is not None and assessment[field] not in result[kind]:
                reject('reference', f'Аттестация ссылается на неизвестный {field}')
        if result['course'][assessment['course_id']]['period_id'] != assessment['period_id']:
            reject('reference', 'Период аттестации и дисциплины различаются')
        period = result['period'][assessment['period_id']]
        start, end = instant(assessment['starts_at']), instant(assessment['ends_at'])
        if not day(period['date_from']) <= start.astimezone(MOSCOW).date() < day(period['date_until']):
            reject('interval', 'Аттестация вне учебного периода')
        if not batch.valid_from <= start < end <= batch.valid_until:
            reject('interval', 'Аттестация вне valid interval набора')
    active = [row for row in assessments if row['status'] == 'active']
    for i, left in enumerate(active):
        for right in active[i + 1:]:
            same_scope = left['group_id'] == right['group_id'] or (left['instructor_id'] is not None and left['instructor_id'] == right['instructor_id'])
            if same_scope and instant(left['starts_at']) < instant(right['ends_at']) and instant(right['starts_at']) < instant(left['ends_at']):
                reject('overlap', 'Пересечение активных аттестаций группы или преподавателя')
    for spec in batch.academic_coverage:
        if spec.student_id not in result['profile'] or spec.period_id not in result['period']:
            reject('reference', 'Учебная полнота ссылается на неизвестного студента/период')
        eligible = {row['source_id']:row for row in enrollments.values() if row['student_id'] == spec.student_id
            and row['period_id'] == spec.period_id and row['status'] == 'active'}
        if spec.entity_type == 'enrollment':
            actual = sorted(eligible)
        elif spec.entity_type == 'assessment':
            courses = {row['course_id'] for row in eligible.values()}
            group = result['profile'][spec.student_id]['group_id']
            actual = sorted(row['source_id'] for row in assessments if row['period_id'] == spec.period_id
                and row['group_id'] == group and row['course_id'] in courses)
        else:
            actual = sorted(row['source_id'] for row in result[spec.entity_type].values() if row['enrollment_id'] in eligible)
        if spec.completeness == 'complete' and (actual != list(spec.record_ids) or len(actual) != spec.record_count):
            reject('coverage', 'Complete academic_coverage не совпадает с итоговыми разрешёнными IDs/count, включая сохранённые записи')

def validate_time(batch, head):
    if batch.source_as_of > timezone.now():
        reject('future_source','source_as_of позже реального времени сервера')
    if head.current_revision:
        previous = ImportRevision.objects.get(dataset=head,revision=head.current_revision)
        if batch.payload['schema_version'] < previous.schema_version:
            reject('schema','Понижение принятой версии схемы запрещено')
        if batch.source_as_of < previous.source_as_of:
            reject('source_time','source_as_of не может двигаться назад')

def get_head(*, lock=False):
    query = Dataset.objects.select_for_update() if lock else Dataset.objects
    try:
        return query.get(pk=DATASET_ID)
    except Dataset.DoesNotExist:
        reject('not_initialized','Сначала выполните initialize_import')

def preview(batch, *, expected_revision):
    batch = validate(batch.payload)
    with read_snapshot():
        head = get_head()
        check_integrity(head)
        outcome = check_head(head,batch,expected_revision)
        if outcome == 'noop':
            return {'outcome':'noop','revision':head.current_revision,'changes':[]}
        validate_time(batch,head)
        _, diff = proposed_state(batch,records(batch.payload['schema_version']))
        return {'outcome':'preview','revision':batch.revision,'changes':diff}

def model_values(kind, row):
    values = row.copy()
    for field, related_kind in [('faculty_id','faculty'),('programme_id','programme'),('group_id','group'),('student_id','profile'),('course_id','course'),('period_id','period'),('instructor_id','instructor'),('enrollment_id','enrollment'),('result_id','final_result')]:
        if field in values and values[field] is not None:
            values[field] = MODELS[related_kind].objects.get(dataset_id=DATASET_ID,source_id=values[field]).pk
    return values

def apply(batch, *, expected_revision, operator):
    batch = validate(batch.payload)  # apply принимает собственное решение, независимо от preview
    string(operator,'operator')
    with transaction.atomic(durable=True):
        head = get_head(lock=True)
        check_integrity(head)
        outcome = check_head(head,batch,expected_revision)
        if outcome == 'noop':
            return {'outcome':'noop','revision':head.current_revision}
        validate_time(batch,head)
        # Все mutations присоединяются к общему протоколу до target locks.
        from accounts.access import lock_guard
        from accounts.models import AccessAudit
        guard = lock_guard()
        list(StudentProfile.objects.filter(dataset=head).order_by('pk').select_for_update())
        list(Lesson.objects.filter(dataset=head).order_by('source_id').select_for_update())
        list(Announcement.objects.filter(dataset=head).order_by('source_id').select_for_update())
        for kind in sorted(ACADEMIC_KINDS):
            list(MODELS[kind].objects.filter(dataset=head).order_by('pk').select_for_update())
        _, diff = proposed_state(batch,records(batch.payload['schema_version']))
        now = timezone.now()
        revision = ImportRevision.objects.create(dataset=head,revision=batch.revision,parent_revision=batch.parent,
            content_hash=batch.payload['content_sha256'],schema_version=batch.payload['schema_version'],provenance='synthetic',
            prepared_by=batch.payload['prepared_by'],preparation_reason=batch.payload['preparation_reason'],
            source_as_of=batch.source_as_of,valid_from=batch.valid_from,valid_until=batch.valid_until,
            imported_at=now,operator=operator)
        # Стабильный порядок создания зависимостей; изменения используют уже существующие ссылки.
        for kind in MODELS:
            for op in sorted((op for op in batch.operations if op.kind == kind),key=lambda op:op.after['source_id']):
                values = model_values(kind,op.after)
                if kind in ACADEMIC_KINDS:
                    if op.action == 'create':
                        target = MODELS[kind].objects.create(dataset=head, **values)
                    else:
                        target = MODELS[kind].objects.get(dataset=head, source_id=op.after['source_id'])
                        for key, value in values.items(): setattr(target, key, value)
                        target.save()
                    history_model, target_field = ACADEMIC_HISTORY[kind]
                    change = history_model.objects.create(dataset=head, revision=revision, **{target_field:target},
                        event_id=op.event_id, change_type=op.change_type, before=op.before, after=op.after,
                        reason=op.reason, operator=operator, source_updated_at=op.after['source_updated_at'], recorded_at=now)
                    if kind != 'enrollment':
                        writer = {'final_result':record_final_result_change, 'requirement':record_requirement_change,
                                  'assessment':record_assessment_change}[kind]
                        writer(change, published_at=now)
                elif kind == 'announcement':
                    if op.action == 'create':
                        announcement = Announcement.objects.create(dataset=head,**values)
                    else:
                        announcement = Announcement.objects.get(dataset=head,source_id=op.after['source_id'])
                        for key,value in values.items(): setattr(announcement,key,value)
                        announcement.save()
                        # Withdrawal and publication changes affect visibility. Invalidate
                        # in-flight snapshots through the existing guard, without touching
                        # identities, sessions, roles, passwords or saved Read records.
                        guard.revision += 1
                        guard.save(update_fields=['revision'])
                        AccessAudit.objects.create(action='announcement_visibility',revision=guard.revision,operator=operator)
                    change = AnnouncementChange.objects.create(dataset=head,revision=revision,announcement=announcement,
                        event_id=op.event_id,change_type=op.change_type,before=op.before,after=op.after,
                        reason=op.reason,operator=operator,source_updated_at=op.after['source_updated_at'],recorded_at=now)
                    published = instant(op.after['published_at'])
                    record_announcement_change(change,published_at=published if op.action == 'create' else max(published,now))
                elif op.action == 'create':
                    MODELS[kind].objects.create(dataset=head,**values)
                elif kind == 'profile':
                    profile = StudentProfile.objects.get(dataset=head,source_id=op.after['source_id'])
                    for key,value in values.items(): setattr(profile,key,value)
                    profile.scope_revision += 1
                    profile.save()
                    ProfileChange.objects.create(dataset=head,revision=revision,profile=profile,event_id=op.event_id,
                        before=op.before,after=op.after,reason=op.reason,operator=operator,recorded_at=now)
                    guard.revision += 1
                    guard.save(update_fields=['revision'])
                    AccessAudit.objects.create(action='profile_group_correction',revision=guard.revision,operator=operator)
                else:
                    lesson = Lesson.objects.get(dataset=head,source_id=op.after['source_id'])
                    for key,value in values.items():
                        setattr(lesson,key,value)
                    lesson.save()
                    change = ScheduleChange.objects.create(dataset=head,revision=revision,event_id=op.event_id,
                        lesson=lesson,change_type=op.change_type,before=op.before,after=op.after,reason=op.reason,
                        operator=operator,source_updated_at=op.after['source_updated_at'],recorded_at=now)
                    record_schedule_change(change,published_at=now)
        for spec in batch.coverage:
            Coverage.objects.create(revision=revision,
                group=MODELS['group'].objects.get(dataset=head,source_id=spec.group_id),
                period=MODELS['period'].objects.get(dataset=head,source_id=spec.period_id),
                date_from=spec.date_from,date_until=spec.date_until,completeness=spec.completeness,
                active_ids=list(spec.active_ids) if spec.active_ids is not None else None,active_count=spec.active_count)
        for spec in batch.academic_coverage:
            AcademicCoverage.objects.create(revision=revision,
                student=MODELS['profile'].objects.get(dataset=head, source_id=spec.student_id),
                period=MODELS['period'].objects.get(dataset=head, source_id=spec.period_id),
                entity_type=spec.entity_type, completeness=spec.completeness,
                record_ids=list(spec.record_ids) if spec.record_ids is not None else None, record_count=spec.record_count)
        # Enrollment changes affect the audience itself. Advance the profile and
        # common revision in the same transaction without touching session/User/Read.
        enrollment_students = {op.after['student_id'] for op in batch.operations if op.kind == 'enrollment'}
        for profile in StudentProfile.objects.filter(dataset=head, source_id__in=enrollment_students).order_by('pk'):
            profile.scope_revision += 1
            profile.save(update_fields=['scope_revision'])
        if any(op.kind in ACADEMIC_KINDS for op in batch.operations):
            guard.revision += 1
            guard.save(update_fields=['revision'])
            AccessAudit.objects.create(action='academic_import', revision=guard.revision, operator=operator)
        head.current_revision = batch.revision
        head.state_hash = state_hash()
        head.save(update_fields=['current_revision','state_hash'])
        if any(op.kind in ('profile', 'enrollment') for op in batch.operations):
            # Newly permitted historical events enter the inbox now, without email replay.
            from accounts.models import Identity
            from notifications.delivery import sync_recipient
            for user_id in Identity.objects.filter(role='student',profile__dataset=head).values_list('user_id',flat=True):
                sync_recipient(user_id)
    return {'outcome':'applied','revision':batch.revision,'changes':len(diff)}
