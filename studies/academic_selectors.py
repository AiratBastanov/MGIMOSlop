"""S4: scoped, материализованные учебные факты для Study/Today/уведомлений.

Вызывается внутри private_snapshot. Все времена предметной области используют
переданный now; безопасность повторно проверяется владельцем snapshot снаружи.
"""
from django.db.models import Exists, OuterRef, F
from data_import.models import Dataset, ImportRevision, ImportAttempt, AcademicCoverage
from .models import (Period, Enrollment, FinalResult, OutstandingRequirement,
                     AssessmentEvent, EnrollmentChange, FinalResultChange,
                     RequirementChange, AssessmentChange, Instructor)
from .selectors import moscow, lesson_values, history_values, exact_moscow


DOMAINS = ('enrollment', 'final_result', 'requirement', 'assessment')
DOMAIN_LABELS = {'enrollment': 'Список дисциплин', 'final_result': 'Результаты',
                 'requirement': 'Учебные требования', 'assessment': 'Сессия'}


def enrollment_scope(actor):
    dataset_id = actor.identity.profile.dataset_id
    return Enrollment.objects.filter(dataset_id=dataset_id, student_id=actor.profile_id,
        student__dataset_id=dataset_id, status='active', course__dataset_id=dataset_id,
        period__dataset_id=dataset_id, course__period_id=F('period_id'))


def assessment_scope(actor):
    dataset_id = actor.identity.profile.dataset_id
    eligible = enrollment_scope(actor).filter(course_id=OuterRef('course_id'), period_id=OuterRef('period_id'))
    return AssessmentEvent.objects.filter(dataset_id=dataset_id, group_id=actor.group_id,
        group__dataset_id=dataset_id, course__dataset_id=dataset_id,
        period__dataset_id=dataset_id, course__period_id=F('period_id')).annotate(
            eligible=Exists(eligible)).filter(eligible=True)


def result_values(record):
    """Принимает current model либо immutable source payload."""
    def value(key):
        return record.get(key) if isinstance(record, dict) else getattr(record, key)
    kind, grade, passed = value('result_kind'), value('grade'), value('pass_status')
    label = str(grade) if kind == 'grade' else ('Зачтено' if passed == 'passed' else 'Не зачтено')
    recorded = value('recorded_at')
    return {'label': label, 'kind': kind, 'grade': grade, 'pass_status': passed,
            'positive': grade in (3, 4, 5) if kind == 'grade' else passed == 'passed',
            'recorded_at': moscow(recorded), 'recorded_exact': exact_moscow(recorded), 'source_id': value('source_id'),
            'record_revision': value('record_revision')}


def requirement_values(record, now, *, enrollment=None):
    def value(key):
        return record.get(key) if isinstance(record, dict) else getattr(record, key)
    due = value('due_at')
    due = moscow(due) if due else None
    closed = value('closed_at')
    status = value('status')
    overdue = status == 'open' and due is not None and now > due
    row = {'source_id': value('source_id'), 'record_revision': value('record_revision'),
           'reason': value('reason'), 'status': status,
           'status_label': 'Закрыто' if status == 'closed' else ('Срок прошёл · требование открыто' if overdue else 'Открыто'),
           'opened_at': moscow(value('opened_at')), 'due_at': due,
           'opened_exact': exact_moscow(value('opened_at')),
           'due_exact': exact_moscow(due) if due else '',
           'closed_at': moscow(closed) if closed else None,
           'closed_exact': exact_moscow(closed) if closed else '',
           'due_label': 'Срок не указан' if due is None else ('Срок прошёл' if overdue else 'Срок'),
           'overdue': overdue}
    if not isinstance(record, dict):
        row['id'] = record.pk
    if enrollment:
        row.update(enrollment_id=enrollment['id'], course_name=enrollment['course_name'],
                   url=enrollment['url'] + '#requirements')
    return row


def assessment_values(event):
    row = lesson_values(event)
    row.update(url=f'/assessments/{event.pk}/', assessment_kind=event.assessment_kind,
               kind_label='Экзамен' if event.assessment_kind == 'exam' else 'Зачёт',
               status_label='Отменён' if event.status == 'cancelled' else 'По расписанию',
               change=None)
    return row


def assessment_history_values(change, current, instructors):
    if change.before:
        row = history_values(change, current, instructors)
    else:
        # Создание — историческое событие; текущая строка не подменяет его payload.
        class Created:
            pass
        clone = Created()
        for attr in ('event_id', 'change_type', 'reason', 'after', 'recorded_at', 'source_updated_at'):
            setattr(clone, attr, getattr(change, attr))
        clone.before = change.after
        row = history_values(clone, current, instructors)
        row['before'] = None
    row['title'] = {'create': 'Событие сессии добавлено', 'move': 'Событие сессии перенесено',
                    'cancel': 'Событие сессии отменено', 'substitute': 'Замена преподавателя',
                    'correction': 'Уточнение события сессии'}.get(change.change_type, 'Изменение сессии')
    row['kind'] = 'assessment'
    return row


def change_differences(before, after, *, occurrence=False):
    """Отображает только изменившиеся поля; unchanged context показывается один раз."""
    if occurrence:
        fields = (('starts_at', 'Начало', exact_moscow),
                  ('ends_at', 'Окончание', exact_moscow),
                  ('room_label', 'Место', str), ('instructor_name', 'Преподаватель', str),
                  ('status_label', 'Статус', str))
    else:
        fields = (('label', 'Результат', str), ('reason', 'Основание', str),
                  ('status_label', 'Статус', str),
                  ('due_at', 'Срок', lambda x: exact_moscow(x) if x else 'Срок не указан'))
    result = []
    for key, label, formatter in fields:
        if key not in after:
            continue
        old, new = before.get(key) if before else None, after.get(key)
        if before is not None and old == new:
            continue
        # Создание assessment уже имеет полный occurrence; не повторять его поля.
        if before is None and occurrence:
            continue
        result.append({'label': label, 'before': formatter(old) if before else 'Не было записи',
                       'after': formatter(new)})
    return result


def academic_snapshot(actor, now):
    dataset_id = actor.identity.profile.dataset_id
    head = Dataset.objects.filter(pk=dataset_id).first()
    revision = (ImportRevision.objects.filter(dataset_id=dataset_id, revision=head.current_revision).first()
                if head and head.current_revision else None)
    attempt = ImportAttempt.objects.filter(recorded_at__gte=revision.imported_at).order_by('-recorded_at', '-id').first() if revision else None
    update_status = attempt.outcome if attempt else ('accepted' if revision else 'not_attempted')
    update_message = ('Обновление не применено. Показаны ранее принятые данные.'
                      if update_status in ('rejected', 'invalid_update') else
                      'Источник обновления недоступен. Показаны ранее принятые данные.'
                      if update_status in ('source_unavailable', 'unavailable') else '')
    if revision is None or revision.schema_version < 4:
        state, message = 'not_prepared', 'Учебные данные ещё не подготовлены.'
    elif now >= revision.valid_until:
        state, message = 'expired', 'Срок действия учебных данных истёк. Ниже — сохранённые сведения, не текущая учебная сводка.'
    elif now < revision.valid_from:
        state, message = 'not_valid', 'Период действия учебных данных ещё не начался. Ниже — сохранённые сведения.'
    else:
        state, message = 'ready', ''
    source = {'state': state, 'message': message, 'update_status': update_status,
              'update_message': update_message, 'source_as_of': moscow(revision.source_as_of) if revision else None,
              'imported_at': moscow(revision.imported_at) if revision else None,
              'valid_from': moscow(revision.valid_from) if revision else None,
              'valid_until': moscow(revision.valid_until) if revision else None,
              'revision': revision.revision if revision else 0}
    coverage = list(AcademicCoverage.objects.filter(revision=revision, student_id=actor.profile_id,
        student__dataset_id=dataset_id, period__dataset_id=dataset_id).values(
            'period_id', 'entity_type', 'completeness')) if revision and revision.schema_version >= 4 else []
    enrolled = list(enrollment_scope(actor).select_related('course', 'period').order_by('course__name', 'source_id'))
    enrollment_ids = [row.pk for row in enrolled]
    period_ids = {row.period_id for row in enrolled} | {row['period_id'] for row in coverage}
    periods = list(Period.objects.filter(dataset_id=dataset_id, pk__in=period_ids).order_by('date_from', 'source_id').values(
        'id', 'source_id', 'name', 'date_from', 'date_until'))
    results = {row.enrollment_id: row for row in FinalResult.objects.filter(dataset_id=dataset_id,
        enrollment_id__in=enrollment_ids, enrollment__student_id=actor.profile_id).order_by('source_id')}
    requirements = list(OutstandingRequirement.objects.filter(dataset_id=dataset_id,
        enrollment_id__in=enrollment_ids, enrollment__student_id=actor.profile_id).order_by('due_at', 'source_id'))
    courses = []
    for enrollment in enrolled:
        complete_results = any(x['period_id'] == enrollment.period_id and x['entity_type'] == 'final_result'
                               and x['completeness'] == 'complete' for x in coverage)
        result = result_values(results[enrollment.pk]) if enrollment.pk in results else None
        course = {'id': enrollment.pk, 'source_id': enrollment.source_id, 'period_id': enrollment.period_id,
                  'course_name': enrollment.course.name, 'course_id': enrollment.course_id,
                  'url': f'/study/enrollments/{enrollment.pk}/', 'result': result,
                  'result_label': result['label'] if result else (
                      'Итог не внесён' if complete_results and state == 'ready' else
                      'Итог неизвестен · источник неполон' if state == 'ready' else 'Итог отсутствует в сохранённых сведениях'),
                  'record_revision': enrollment.record_revision}
        course['requirements'] = [requirement_values(row, now, enrollment=course) for row in requirements
                                  if row.enrollment_id == enrollment.pk]
        course['open_requirements'] = sorted((x for x in course['requirements'] if x['status'] == 'open'),
            key=lambda row: (not row['overdue'], row['due_at'] is None, row['due_at'] or now, row['source_id']))
        course['has_open'] = bool(course['open_requirements'])
        course['overdue_count'] = sum(row['overdue'] for row in course['open_requirements'])
        course['classification'] = 'open' if course['has_open'] else ('positive' if result and result['positive'] else 'unentered' if result is None else 'inconsistent')
        courses.append(course)
    course_map = {row['id']: row for row in courses}
    events = list(assessment_scope(actor).select_related('course', 'instructor').order_by('starts_at', 'source_id'))
    assessments = [assessment_values(event) for event in events]
    from profiles.selectors import person_for_instructor
    for event, row in zip(events, assessments):
        row['instructor_person'] = person_for_instructor(actor,event.instructor_id,row['instructor_name'])
    for row in assessments:
        row['phase'] = 'past' if row['ends_at'] <= now else 'current' if row['starts_at'] <= now else 'upcoming'
    event_map = {row['id']: row for row in assessments}
    changes = list(AssessmentChange.objects.filter(dataset_id=dataset_id,
        assessment_id__in=event_map).order_by('recorded_at', 'event_id'))
    instructor_ids = {record.get('instructor_id') for change in changes for record in (change.before or {}, change.after)}
    instructors = dict(Instructor.objects.filter(dataset_id=dataset_id, source_id__in=instructor_ids).values_list('source_id', 'name'))
    assessment_history = []
    for change in changes:
        row = assessment_history_values(change, event_map[change.assessment_id], instructors)
        row['differences'] = change_differences(row['before'], row['after'], occurrence=True)
        assessment_history.append(row)
        if not row['is_historical'] and change.before:
            # Краткое изменение — скаляры; current не включаем, чтобы не создать цикл.
            event_map[change.assessment_id]['change'] = {key: row[key] for key in
                ('event_id', 'title', 'reason', 'before', 'after', 'differences', 'published_at')}
    history = []
    history_specs = ((EnrollmentChange, 'enrollment', 'enrollment_id'),
                     (FinalResultChange, 'result', 'result__enrollment_id'),
                     (RequirementChange, 'requirement', 'requirement__enrollment_id'))
    for model, kind, lookup in history_specs:
        query = model.objects.filter(dataset_id=dataset_id, **{lookup + '__in': enrollment_ids}).order_by('recorded_at', 'event_id')
        if kind == 'enrollment':
            entries = [(change, change.enrollment_id) for change in query]
        else:
            entries = [(change, getattr(change, kind).enrollment_id) for change in query.select_related(kind)]
        for change, enrollment_id in entries:
            course = course_map[enrollment_id]
            if kind == 'result':
                before = result_values(change.before) if change.before else None
                after = result_values(change.after)
                title = 'Итог внесён' if before is None else 'Итог исправлен'
                current_revision = results[enrollment_id].record_revision
            elif kind == 'requirement':
                # История описывает факт на момент публикации; сегодняшний
                # overdue вычисляется отдельно у текущего требования.
                before = requirement_values(change.before, change.recorded_at) if change.before else None
                after = requirement_values(change.after, change.recorded_at)
                for payload in (before, after):
                    if payload:
                        payload['status_label'] = 'Открыто' if payload['status'] == 'open' else 'Закрыто'
                        payload['due_label'] = 'Срок' if payload['due_at'] else 'Срок не указан'
                        payload['overdue'] = False
                title = 'Учебное требование добавлено' if before is None else 'Учебное требование изменено'
                current_revision = getattr(change, kind).record_revision
            else:
                before = {'status_label': 'Назначена' if change.before['status'] == 'active' else 'Назначение отозвано'} if change.before else None
                after = {'status_label': 'Назначена' if change.after['status'] == 'active' else 'Назначение отозвано'}
                title, current_revision = 'Назначение дисциплины', course['record_revision']
            history.append({'kind': kind, 'event_id': change.event_id, 'enrollment_id': enrollment_id,
                'title': title, 'before': before, 'after': after, 'reason': change.reason,
                'published_at': moscow(change.recorded_at), 'source_updated_at': moscow(change.source_updated_at),
                'is_historical': change.after['record_revision'] != current_revision,
                'current': course, 'url': course['url'],
                'differences': change_differences(before, after)})
    history.sort(key=lambda row: (row['published_at'], row['event_id']))
    return {'source': source, 'coverage': coverage, 'periods': periods, 'courses': courses,
            'assessments': assessments, 'history': history, 'assessment_history': assessment_history, 'now': now}


def study_view(snapshot, *, period_id=None, view='results', filter_name='all'):
    """URL period — source_id, разрешённые периоды получены только из scope."""
    from accounts.access import AccessDenied
    periods, now = snapshot['periods'], snapshot['now']
    if period_id == 'all':
        views = [study_view(snapshot, period_id=period['source_id'], view=view, filter_name=filter_name) for period in periods]
        courses = [dict(course, period_name=row['period']['name']) for row in views for course in row['courses']]
        complete = bool(views) and all(row['summary']['complete'] for row in views)
        summary = {'complete': complete,
            **{key: sum(row['summary'][key] or 0 for row in views) if complete else None for key in ('N','X','Y','Z')},
            **{key: sum(row['summary'][key] for row in views) for key in ('known_courses','known_open_courses','known_open_requirements','known_overdue_requirements')},
            'message': ' '.join(dict.fromkeys(row['summary']['message'] for row in views if row['summary']['message'])),
            'completeness': {}}
        upcoming = sorted([event for row in views for event in row['upcoming_assessments']], key=lambda event:(event['starts_at'],event['id']))
        return {'period': {'source_id':'all','name':'Все доступные периоды'}, 'periods':periods,
            'is_all_periods': True, 'summary':summary, 'courses':courses,
            'course_count':sum(row['course_count'] for row in views), 'filtered_count':len(courses),
            'view':view,'study_filter':filter_name,'upcoming_assessments':upcoming,
            'past_assessments':[event for row in views for event in row['past_assessments']],
            'next_assessment':next((event for event in upcoming if event['status']=='active'),None),
            'assessment_complete':bool(views) and all(row['assessment_complete'] for row in views),
            'assessment_message':' '.join(dict.fromkeys(row['assessment_message'] for row in views if row['assessment_message'])),
            'next_assessment_label':'Ближайшее известное событие сессии'}
    if period_id is not None:
        period = next((row for row in periods if row['source_id'] == period_id), None)
        if period is None:
            raise AccessDenied('not_found', 404)
    else:
        period = next((row for row in periods if row['date_from'] <= moscow(now).date() < row['date_until']), None)
        period = period or (periods[-1] if periods else None)
    pid = period['id'] if period else None
    completeness = {domain: next((row['completeness'] for row in snapshot['coverage']
        if row['period_id'] == pid and row['entity_type'] == domain), 'unknown') for domain in DOMAINS}
    current = snapshot['source']['state'] == 'ready'
    courses = [row for row in snapshot['courses'] if row['period_id'] == pid]
    counts = {key: sum(row['classification'] == kind for row in courses)
              for key, kind in (('X', 'positive'), ('Y', 'open'), ('Z', 'unentered'))}
    consistent = sum(counts.values()) == len(courses)
    complete = current and all(completeness[key] == 'complete' for key in ('enrollment', 'final_result', 'requirement')) and consistent
    incomplete = [DOMAIN_LABELS[key] for key in ('enrollment', 'final_result', 'requirement') if completeness[key] != 'complete']
    message = snapshot['source']['message']
    if current and not complete:
        message = ('Данные неполные: ' + ', '.join(incomplete).lower() + '. Точная сводка недоступна.'
                   if incomplete else 'Учебные сведения противоречат друг другу. Точная сводка недоступна.')
    summary = {'complete': complete, 'N': len(courses) if complete else None,
               **{key: value if complete else None for key, value in counts.items()},
               'known_courses': len(courses), 'known_open_courses': counts['Y'],
               'known_open_requirements': sum(len(row['open_requirements']) for row in courses),
               'known_overdue_requirements': sum(row['overdue_count'] for row in courses),
               'message': message, 'completeness': completeness}
    selected = [row for row in courses if filter_name == 'all' or
        (filter_name == 'open' and row['has_open']) or (filter_name == 'overdue' and row['overdue_count']) or
        (filter_name == 'unentered' and row['result'] is None) or
        (filter_name == 'assessed' and row['result'] is not None)]
    events = [row for row in snapshot['assessments'] if row['period_id'] == pid]
    upcoming = [row for row in events if row['ends_at'] > now]
    past = [row for row in events if row['ends_at'] <= now]
    eligible_next = next((row for row in upcoming if row['status'] == 'active'), None) if current else None
    assessment_complete = current and completeness['enrollment'] == completeness['assessment'] == 'complete'
    assessment_message = snapshot['source']['message']
    if current and not assessment_complete:
        assessment_message = 'Список сессии неполон. Показаны только известные события по доступным назначениям дисциплин.'
    return {'period': period, 'periods': periods, 'summary': summary, 'courses': selected,
            'course_count': len(courses), 'filtered_count': len(selected), 'view': view,
            'study_filter': filter_name, 'upcoming_assessments': upcoming, 'past_assessments': past,
            'next_assessment': eligible_next, 'assessment_complete': assessment_complete,
            'assessment_message': assessment_message,
            'next_assessment_label': 'Ближайшее событие сессии' if assessment_complete else 'Ближайшее известное событие сессии'}
