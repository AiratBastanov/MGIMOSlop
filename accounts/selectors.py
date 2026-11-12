"""Личные составные чтения: один снимок/now, затем fresh access check."""
from datetime import timedelta
import calendar
from django.db import transaction
from django.db.models import Exists, OuterRef, Q, F, IntegerField
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
from django.utils import timezone
from data_import.services import read_snapshot
from data_import.models import ImportRevision
from notifications.models import Notification, NotificationRead, Announcement
from studies.selectors import schedule_snapshot, schedule_day, next_lesson, moscow, exact_moscow
from studies.academic_selectors import (academic_snapshot, study_view, enrollment_scope,
    assessment_scope, change_differences)
from student_requests.selectors import request_scope, request_values, request_page
from student_requests.workflow import STATUS_LABELS
from .access import current_actor, lock_guard, AccessDenied
from .models import StaffScope


def read_now():
    """Одна реальная метка времени фактов; test clock patches только этой функции."""
    return timezone.now()


def valid_source(actor, now):
    return Exists(ImportRevision.objects.filter(dataset_id=actor.identity.profile.dataset_id,
        revision=F('dataset__current_revision'),valid_from__lte=now,valid_until__gt=now))


def announcement_audience(actor, prefix=''):
    profile = actor.identity.profile
    def key(name):
        return prefix + name
    return (Q(**{key('audience'):'all_students',key('faculty__isnull'):True,
                 key('group__isnull'):True,key('student__isnull'):True}) |
            Q(**{key('audience'):'faculty',key('faculty_id'):profile.group.programme.faculty_id,
                 key('group__isnull'):True,key('student__isnull'):True}) |
            Q(**{key('audience'):'group',key('group_id'):actor.group_id,
                 key('faculty__isnull'):True,key('student__isnull'):True}) |
            Q(**{key('audience'):'student',key('student_id'):actor.profile_id,
                 key('faculty__isnull'):True,key('group__isnull'):True}))


def visible_announcements(actor, *, now, archive=False):
    if actor.role != 'student':
        raise AccessDenied('forbidden',403)
    query = Announcement.objects.filter(dataset_id=actor.identity.profile.dataset_id,
        published_at__lte=now).filter(announcement_audience(actor)).exclude(status='draft').annotate(accepted_valid=valid_source(actor,now))
    if not archive:
        query = query.filter(status='published',accepted_valid=True).filter(Q(visible_until__isnull=True)|Q(visible_until__gt=now))
    return query


def domain_notifications(actor, *, now=None, archive=False, include_application=True):
    now = actor.checked_at if now is None else now
    from workspaces.events import event_scope
    app_events = Q(application_event__in=event_scope(actor,now,archive)) if include_application else Q(pk__in=[])
    if actor.role != 'student':
        from django.db.models import Value, BooleanField
        own = app_events
        if actor.role == 'staff':
            own |= Q(request_event__request__in=request_scope(actor))
            if not archive:
                own &= (Q(application_event__isnull=False)|Q(request_event__version=F('request_event__request__version')))
        return Notification.objects.filter(own,published_at__lte=now).annotate(accepted_valid=Value(True,output_field=BooleanField()))
    dataset_id = actor.identity.profile.dataset_id
    schedule = Q(group_id=actor.group_id,schedule_change__dataset_id=dataset_id,
        schedule_change__lesson__dataset_id=dataset_id,schedule_change__lesson__group_id=actor.group_id,
        announcement_change__isnull=True)
    source = 'announcement_change__announcement__'
    announcements = Q(announcement_change__dataset_id=dataset_id,
        announcement_change__announcement__dataset_id=dataset_id,
        announcement_change__announcement__published_at__lte=now,
        announcement_change__announcement__status__in=['published','withdrawn'],
        announcement_change__after__status__in=['published','withdrawn'],schedule_change__isnull=True,
        group__isnull=True) & announcement_audience(actor,source)
    enrollments = enrollment_scope(actor)
    academic = []
    for target, parent in (('final_result_change', 'result'), ('requirement_change', 'requirement')):
        academic.append(Q(**{target + '__dataset_id': dataset_id,
            target + '__' + parent + '__dataset_id': dataset_id,
            target + '__' + parent + '__enrollment__in': enrollments,
            'group__isnull': True}))
    assessment = Q(assessment_change__dataset_id=dataset_id,
        assessment_change__assessment__in=assessment_scope(actor), group__isnull=True)
    # Заявка принадлежит приложению. Полнота и validity академического источника
    # не отзывают доступ к сохранённому обращению или его статусу.
    requests = Q(request_event__request__in=request_scope(actor))
    query = Notification.objects.filter(published_at__lte=now).annotate(
        announcement_version=Cast(KeyTextTransform('record_revision','announcement_change__after'),IntegerField()),
        result_version=Cast(KeyTextTransform('record_revision','final_result_change__after'),IntegerField()),
        requirement_version=Cast(KeyTextTransform('record_revision','requirement_change__after'),IntegerField()),
        assessment_version=Cast(KeyTextTransform('record_revision','assessment_change__after'),IntegerField()),
        accepted_valid=valid_source(actor,now))
    if not archive:
        announcements &= (Q(announcement_change__announcement__status='published') &
                          (Q(announcement_change__announcement__visible_until__isnull=True)|
                           Q(announcement_change__announcement__visible_until__gt=now)) &
                          Q(announcement_version=F('announcement_change__announcement__record_revision')))
        academic[0] &= Q(result_version=F('final_result_change__result__record_revision'))
        academic[1] &= Q(requirement_version=F('requirement_change__requirement__record_revision'))
        assessment &= Q(assessment_version=F('assessment_change__assessment__record_revision'))
        requests &= Q(request_event__version=F('request_event__request__version'))
    source_events = schedule | announcements | academic[0] | academic[1] | assessment
    if not archive:
        source_events &= Q(accepted_valid=True)
    return query.filter(source_events | requests | app_events)


def visible_notifications(actor, *, now=None, archive=False):
    if archive:
        return domain_notifications(actor, now=now, archive=True)
    from notifications.selectors import inbox_events
    return inbox_events(actor, now=now or actor.checked_at)


def announcement_values(item, now, *, source_valid=None):
    if source_valid is None:
        source_valid = item.accepted_valid
    available = source_valid and item.status == 'published' and (item.visible_until is None or now < item.visible_until)
    expired_deadline = item.due_at is not None and now > item.due_at
    if item.status == 'withdrawn':
        status_label = 'Объявление отозвано'
    elif not source_valid:
        status_label = 'Архив · актуальность данных не подтверждена'
    elif not available:
        status_label = 'Архив · период публикации завершён'
    else:
        status_label = 'Опубликовано'
    return {'id':item.pk,'url':f'/announcements/{item.pk}/','title':item.title,
            'body':item.body if item.status != 'withdrawn' else '',
            'published_at':moscow(item.published_at),
            'visible_until':moscow(item.visible_until) if item.visible_until else None,
            'due_at':moscow(item.due_at) if item.due_at else None,
            'due_exact':exact_moscow(item.due_at) if item.due_at else '',
            'deadline_label':'Срок объявления истёк' if expired_deadline else ('Срок объявления' if item.due_at else ''),
            'deadline_elapsed':expired_deadline,'status':item.status,'status_label':status_label,
            'available':available,'audience':item.audience}


def occurrence_context(before, after):
    """Только неизменившийся контекст события; дата публикации идёт отдельно."""
    labels = []
    if before is None or (before['starts_at'], before['ends_at']) == (after['starts_at'], after['ends_at']):
        labels.append(f"{after['starts_at']:%d.%m.%Y} · {after['time_label']} МСК")
    if before is None or before['room_label'] == after['room_label']:
        labels.append(after['room_label'])
    if before is None or before['instructor_name'] == after['instructor_name']:
        labels.append(after['instructor_name'])
    return labels


def notification_values(notification, now, schedule, academic=None):
    if notification.application_event_id:
        from workspaces.events import values
        return values(notification,now)
    base = {'id':notification.pk,'published_at':moscow(notification.published_at),
            'is_read':notification.is_read,'detail_url':f'/notifications/{notification.pk}/',
            'read_url':f'/notifications/{notification.pk}/read/','available':notification.accepted_valid,
            'can_read':notification.accepted_valid,
            'deadline_label':'','before':None,'after':None,'differences':[], 'context_labels':[],
            'source_message':'' if notification.accepted_valid else 'Архив · актуальность источника не подтверждена'}
    if notification.request_event_id:
        event = notification.request_event
        current = request_values(event.request)
        historical = event.version != event.request.version
        base.update(kind='request', event_id=f'request:{event.request_id}:v{event.version}',
            title='Статус заявки', reference=current['reference'], type_label=current['type_label'],
            current=current, event_status=event.to_status, event_status_label=STATUS_LABELS[event.to_status],
            explanation=event.explanation, is_historical=historical,
            target_url=f'/requests/{event.request_id}/', action_label='Открыть заявку',
            history_url=f'/requests/{event.request_id}/#request-history',
            available=True, can_read=not historical, source_message='',
            ready_notice=current['ready_notice'] if event.to_status == 'ready' else '',
            status_label='', next_step='')
    elif notification.schedule_change_id:
        change = next(row for row in schedule['history'] if row['event_id'] == notification.schedule_change.event_id)
        current = change['current']
        base.update(change)
        base.update(kind='schedule',published_at=moscow(notification.published_at),
            target_url=current['url'],action_label='Текущее занятие',
            history_url=current['url']+'#history-title',
            next_step='', differences=change_differences(change['before'],change['after'],occurrence=True),
            context_labels=occurrence_context(change['before'],change['after']))
    elif notification.announcement_change_id:
        change = notification.announcement_change
        item = announcement_values(change.announcement,now,source_valid=notification.accepted_valid)
        historical = change.after['record_revision'] != change.announcement.record_revision
        event_due = moscow(change.after['due_at']) if change.after.get('due_at') else None
        event_deadline_label = 'Срок объявления истёк' if event_due and now > event_due else ('Срок объявления' if event_due else '')
        base.update(kind='announcement',event_id=change.event_id,title=change.after['title'],current=item,
            target_url=item['url'],action_label='Открыть объявление',is_historical=historical,
            available=item['available'],deadline_label=event_deadline_label,due_at=event_due,
            due_exact=exact_moscow(event_due) if event_due else '',
            can_read=item['available'] and not historical,
            status_label=item['status_label'],
            body=change.after['body'] if item['status'] != 'withdrawn' else '',
            next_step='Объявление отозвано; актуальные условия недоступны.' if item['status'] == 'withdrawn'
                else ('Период публикации завершён. Можно прочитать архив.' if not item['available']
                      else ''))
    else:
        if notification.assessment_change_id:
            change = next(row for row in academic['assessment_history']
                          if row['event_id'] == notification.assessment_change.event_id)
            kind, action = 'assessment', 'Текущее событие сессии'
            context_labels = occurrence_context(change['before'], change['after'])
        else:
            event = notification.final_result_change if notification.final_result_change_id else notification.requirement_change
            change = next(row for row in academic['history'] if row['event_id'] == event.event_id)
            kind, action = change['kind'], 'Результат и требования'
            context_labels = []
        base.update(change)
        base.update(kind=kind, published_at=moscow(notification.published_at), target_url=change['url'],
                    action_label=action, next_step='', context_labels=context_labels,
                    history_url=change['url']+'#history',
                    can_read=notification.accepted_valid and not change['is_historical'])
        period = next(row for row in academic['periods'] if row['id'] == change['current']['period_id'])
        scope_view = study_view(academic, period_id=period['source_id'])
        warning = scope_view['assessment_message'] if kind == 'assessment' else scope_view['summary']['message']
        base['warning'] = warning if notification.accepted_valid else ''
        if kind == 'requirement':
            base.update(due_at=change['after']['due_at'], reason=change['after']['reason'],
                        due_exact=change['after']['due_exact'],
                        deadline_label=change['after']['due_label'])
            before, after = change['before'], change['after']
            if before:
                labels = []
                if before['reason'] == after['reason']:
                    labels.append(after['reason'])
                if before['status'] == after['status']:
                    labels.append(after['status_label'])
                if before['due_at'] == after['due_at']:
                    labels.append('Срок: ' + after['due_exact'] if after['due_at'] else 'Срок не указан')
                base['context_labels'] = labels
        if kind == 'result':
            base['summary'] = 'Итог: ' + change['after']['label']
    return base


def today_attention(academic, changes, announcements, now, tasks=()):
    """Детерминированная подача: просроченные → ближайшие 7 суток → остальное.

    Publication/unread не влияют на срочность. Preview не меняет канонические
    counts. Для каждой категории остаются count и полный scoped destination.
    """
    horizon = now + timedelta(days=7)
    items = list(tasks)
    courses = academic['courses'] if academic['source']['state'] == 'ready' else []
    requirements = [req for course in courses for req in course['open_requirements']]
    for req in requirements:
        due = req['due_at']
        rank = 0 if req['overdue'] else 1 if due and due <= horizon else 2 if due else 3
        items.append({'key': 'requirement:' + req['source_id'], 'rank': rank,
            'sort_at': due or now, 'kind': 'requirement', 'kind_label': 'Учебное требование',
            'title': req['course_name'], 'context': req['status_label'], 'detail': req['reason'],
            'due_at': due, 'due_exact': req['due_exact'], 'due_label': req['due_label'], 'url': req['url'], 'overdue': req['overdue']})
    near_changes = 0
    for change in changes:
        current = change['current']
        affected = [row['starts_at'] for row in (change['before'], change['after'], current)
                    if row and row['ends_at'] > now]
        if not affected:
            continue
        when = min(affected)
        near_changes += when <= horizon
        items.append({'key': 'schedule:' + change['event_id'], 'rank': 1 if when <= horizon else 4,
            'sort_at': when, 'kind': 'schedule', 'kind_label': 'Изменение расписания',
            'title': current['course_name'], 'context': change['title'],
            'detail': f"{current['starts_at']:%d.%m.%Y} · {current['time_label']} МСК · {current['room_label']}",
            'due_at': None, 'due_label': '', 'url': change['target_url'], 'overdue': False,
            'cancelled': current['status'] == 'cancelled',
            'differences': [row for row in change['differences'] if current['status'] != 'cancelled' or row['label'] != 'Статус']})
    academic_change_count = 0
    if academic['source']['state'] == 'ready':
        for change in academic['assessment_history']:
            if change['is_historical'] or change['before'] is None:
                continue
            current = change['current']
            affected = [row['starts_at'] for row in (change['before'], change['after'], current)
                        if row and row['ends_at'] > now]
            if not affected:
                continue
            when = min(affected)
            academic_change_count += 1
            near_changes += when <= horizon
            items.append({'key': 'assessment:' + change['event_id'], 'rank': 1 if when <= horizon else 4,
                'sort_at': when, 'kind': 'assessment', 'kind_label': 'Изменение сессии',
                'title': current['course_name'], 'context': change['title'],
                'detail': f"{current['starts_at']:%d.%m.%Y} · {current['time_label']} МСК · {current['room_label']}",
                'due_at': None, 'due_label': '', 'url': change['url'], 'overdue': False,
                'cancelled': current['status'] == 'cancelled',
                'differences': [row for row in change['differences'] if current['status'] != 'cancelled' or row['label'] != 'Статус']})
    live_deadlines = [item for item in announcements if item['due_at'] and not item['deadline_elapsed']]
    for announcement in live_deadlines:
        due = announcement['due_at']
        items.append({'key': 'announcement:' + str(announcement['id']), 'rank': 1 if due <= horizon else 4,
            'sort_at': due, 'kind': 'announcement', 'kind_label': 'Срок объявления',
            'title': announcement['title'], 'context': 'Информационный срок', 'detail': '',
            'due_at': due, 'due_exact': announcement['due_exact'], 'due_label': 'Срок объявления', 'url': announcement['url'], 'overdue': False})
    items.sort(key=lambda row: (row['rank'], row['sort_at'], row['key']))
    links = []
    if tasks:
        links.append({'label':'Задания, ожидающие отправки или исправления','count':len(tasks),'url':'/study/teaching/?kind=task'})
    for period in academic['periods']:
        own = [req for course in courses if course['period_id'] == period['id'] for req in course['open_requirements']]
        if own:
            links.append({'label': 'Открытые требования · ' + period['name'], 'count': len(own),
                          'url': '/study/?period=' + period['source_id'] + '&filter=open',
                          'overdue_count': sum(req['overdue'] for req in own)})
    if changes:
        links.append({'label': 'Все изменения расписания', 'count': None, 'url': '/notifications/?kind=schedule'})
    if academic_change_count:
        links.append({'label': 'Все изменения учёбы и сессии', 'count': None, 'url': '/notifications/?kind=academic'})
    if live_deadlines:
        links.append({'label': 'Все сроки объявлений', 'count': sum(item['due_at'] is not None for item in announcements), 'url': '/notifications/?kind=deadlines'})
    # Даже если три ближайших элемента одной категории, остальные не исчезают:
    # above-list totals + below-list links честно сообщают о них.
    incomplete = []
    for period in academic['periods']:
        view = study_view(academic, period_id=period['source_id'])
        if not view['summary']['complete']:
            incomplete.append(period['name'] + ': ' + view['summary']['message'])
    warning = academic['source']['message'] or ' '.join(incomplete)
    if not academic['periods'] and not warning:
        warning = 'Учебные данные для вашего профиля не подтверждены. Точная сводка недоступна.'
    return {'attention': items[:3], 'attention_count': len(items), 'attention_overflow': max(0, len(items) - 3),
            'attention_links': links, 'attention_open_count': len(requirements),
            'attention_overdue_count': sum(req['overdue'] for req in requirements),
            'attention_near_change_count': near_changes, 'attention_window_days': 7,
            'academic_warning': warning, 'academic_update_warning': academic['source']['update_message']}


def private_snapshot(request, *, section='profile', profile_id=None, notification_id=None,
                     selected_date=None, mode='day', lesson_id=None, announcement_id=None, archive=False,
                     period_id=None, study_mode='results', study_filter='all', enrollment_id=None,
                     assessment_id=None, notification_kind='all', request_id=None, request_filter='all',
                     request_page_number=1, notification_page_number=1, extension=None, inbox_mode=None, request_queue_filters=None):
    now = read_now()
    with read_snapshot():
        actor = current_actor(request)
        identity = actor.identity
        data = {'role':actor.role,'marker':actor.marker,'expires_at':actor.expires_at.isoformat(),
                'section':section,'rendered_at':moscow(now),'today_date':moscow(now).date()}
        from profiles.selectors import person_for_identity
        data['person'] = person_for_identity(actor, identity)
        if actor.role == 'student':
            if section in ('staff', 'staff_requests', 'staff_request_detail'):
                raise AccessDenied('forbidden',403)
            from studies.models import StudentProfile
            scope = StudentProfile.objects.filter(pk=actor.profile_id)
            profile = scope.filter(pk=profile_id or actor.profile_id).select_related('group__programme__faculty').first()
            if not profile:
                raise AccessDenied('not_found',404)
            data.update(name=profile.name,group=profile.group.name,year=profile.year,
                programme=profile.group.programme.name,faculty=profile.group.programme.faculty.name)
            active_query = visible_notifications(actor,now=now).annotate(is_read=Exists(
                NotificationRead.objects.filter(user_id=actor.user_id,notification_id=OuterRef('pk'))))
            from notifications.selectors import inbox_unread
            data['unread'] = inbox_unread(actor, now)
            data['notifications'] = []
            schedule = None
            academic = None
            if section in ('today','schedule','lesson','notifications'):
                schedule = schedule_snapshot(actor,now)
                data['source'] = schedule['source']
            if section in ('today','study','enrollment','assessment','notifications'):
                academic = academic_snapshot(actor,now)
                data['academic_source'] = academic['source']
                if section in ('study','enrollment','assessment'):
                    data['source'] = academic['source']
                    data['source_messages_shown'] = True
            if section in ('requests', 'request_detail'):
                data.update(request_page(actor,request_id=request_id,status_filter=request_filter,page_number=request_page_number,queue_filters=request_queue_filters))
            elif section == 'request_new':
                from student_requests.services import issue_submission
                data['submission_token'] = issue_submission(actor,payload_schema=2)
            elif section in ('today','schedule'):
                date = selected_date or moscow(now).date()
                data.update(selected_date=date,selected_iso=date.isoformat(),view=mode,
                    today_url='/today/',day_url=f'/schedule/?date={date.isoformat()}&view=day',
                    week_url=f'/schedule/?date={date.isoformat()}&view=week')
                if section == 'today':
                    # Essential academic changes outlive the seven-day inbox policy.
                    active_query = domain_notifications(actor,now=now).annotate(is_read=Exists(
                        NotificationRead.objects.filter(user_id=actor.user_id,notification_id=OuterRef('pk'))))
                    data['day'] = schedule_day(schedule,date)
                    data['spotlight'] = next_lesson(schedule)
                    focus_id = data['spotlight']['lesson']['id'] if data['spotlight']['lesson'] else None
                    data['agenda_lessons'] = [row for row in data['day']['lessons'] if row['id'] != focus_id]
                    data['agenda_label'] = 'Другие занятия сегодня' if focus_id in {row['id'] for row in data['day']['lessons']} else 'План на сегодня'
                    latest_changes = {}
                    events = list(active_query.filter(schedule_change__isnull=False)
                        .select_related('schedule_change').order_by('-published_at','schedule_change__event_id'))
                    for event in events:
                        item = notification_values(event,now,schedule)
                        if any(item[key]['date'] >= moscow(now).date() for key in ('before','after','current')):
                            lesson_id = item['current']['id']
                            # Today — одна последняя публикация на занятие. При
                            # одинаковом publication time побеждает новая версия
                            # факта; вся неизменная цепочка остаётся в журнале.
                            order = (item['published_at'],event.schedule_change.after['record_revision'])
                            if lesson_id not in latest_changes or order > latest_changes[lesson_id][0]:
                                latest_changes[lesson_id] = (order,item)
                    all_changes = sorted((entry[1] for entry in latest_changes.values()),
                        key=lambda item:(min(row['starts_at'] for row in (item['before'],item['after'],item['current'])
                            if row['date'] >= moscow(now).date()),item['event_id']))
                    data['changes'] = all_changes[:3]
                    data['change_count'] = len(all_changes)
                    announcements = [announcement_values(item,now) for item in visible_announcements(actor,now=now)
                                     .order_by('-published_at','source_id')]
                    from workspaces.selectors import student_today
                    tasks,faculty_notices=student_today(actor,now)
                    announcements=faculty_notices+announcements
                    deadlines = sorted((item for item in announcements if item['due_at']),
                                       key=lambda item:(item['deadline_elapsed'],item['due_at'],item['id']))
                    news = [item for item in announcements if item['due_at'] is None]
                    data['deadlines'], data['deadline_count'] = deadlines[:3], len(deadlines)
                    data['announcements'], data['announcement_count'] = news[:2], len(announcements)
                    data.update(today_attention(academic,all_changes,announcements,now,tasks))
                    study = study_view(academic)
                    data.update(next_assessment=study['next_assessment'],
                                next_assessment_label=study['next_assessment_label'],
                                assessment_message=study['assessment_message'], study_period=study['period'])
                    academic_events = list(active_query.filter(Q(final_result_change__isnull=False)|Q(requirement_change__isnull=False)|Q(assessment_change__isnull=False))
                        .select_related('final_result_change__result','requirement_change__requirement','assessment_change__assessment'))
                    academic_events = sorted((notification_values(item,now,schedule,academic) for item in academic_events),
                        key=lambda item:(-item['published_at'].timestamp(),item['event_id']))
                    data['academic_changes'] = academic_events[:2]
                    data['academic_change_count'] = len(academic_events)
                else:
                    start = date - timedelta(days=date.weekday()) if mode == 'week' else date
                    length = 7 if mode == 'week' else 1
                    data['days'] = [schedule_day(schedule,start+timedelta(days=i)) for i in range(length)]
                    data['range_start'],data['range_end'] = start,start+timedelta(days=length-1)
                    data['previous_url'] = f'/schedule/?date={(date-timedelta(days=length)).isoformat()}&view={mode}'
                    data['next_url'] = f'/schedule/?date={(date+timedelta(days=length)).isoformat()}&view={mode}'
                    data['month_days'] = []
                    for number in range(1, calendar.monthrange(date.year,date.month)[1]+1):
                        month_date = date.replace(day=number)
                        day = schedule_day(schedule,month_date)
                        day.update(date_iso=month_date.isoformat(),url=f'/schedule/?date={month_date.isoformat()}&view={mode}')
                        data['month_days'].append(day)
            elif section == 'lesson':
                lesson = next((row for row in schedule['lessons'] if row['id'] == lesson_id),None)
                if lesson is None:
                    raise AccessDenied('not_found',404)
                data['lesson'] = lesson
                data['history'] = [row for row in schedule['history'] if row['current']['id'] == lesson_id]
                data['day_url'] = f'/schedule/?date={lesson["date"].isoformat()}&view=day'
                data['day'] = schedule_day(schedule,lesson['date'])
            elif section == 'announcement':
                item = visible_announcements(actor,now=now,archive=True).filter(pk=announcement_id).first()
                if item is None:
                    raise AccessDenied('not_found',404)
                data['announcement'] = announcement_values(item,now)
            elif section == 'study':
                data.update(study_view(academic,period_id=period_id,view=study_mode,filter_name=study_filter))
            elif section == 'enrollment':
                item = next((row for row in academic['courses'] if row['id'] == enrollment_id),None)
                if item is None:
                    raise AccessDenied('not_found',404)
                selected = next(row for row in academic['periods'] if row['id'] == item['period_id'])
                data.update(study_view(academic,period_id=selected['source_id']))
                data['enrollment'] = item
                data['history'] = [row for row in academic['history'] if row['enrollment_id'] == item['id']]
            elif section == 'assessment':
                item = next((row for row in academic['assessments'] if row['id'] == assessment_id),None)
                if item is None:
                    raise AccessDenied('not_found',404)
                selected = next(row for row in academic['periods'] if row['id'] == item['period_id'])
                data.update(study_view(academic,period_id=selected['source_id'],view='assessments'))
                data['assessment'] = item
                data['history'] = [row for row in academic['assessment_history'] if row['current']['id'] == item['id']]
            elif section == 'notifications':
                from notifications.selectors import inbox_page
                page = inbox_page(actor,now=now,archive=archive,kind=notification_kind,
                    page_number=notification_page_number,notification_id=notification_id,mode=inbox_mode)
                events = page.pop('events')
                data.update(page)
                if notification_id is not None and not events:
                    raise AccessDenied('not_found',404)
                data['notifications'] = [notification_values(item,now,schedule,academic) for item in events]
                for item in data['notifications']:
                    from notifications.inbox_actions import snapshot_token
                    item['can_read'] = data['inbox_mode']!='trash'
                    item['recipient_token']=snapshot_token(actor,[item['id']],'restore' if data['inbox_mode']=='trash' else 'trash')
                data['archive_mode'] = data['inbox_mode']=='history'
                data['notification_kind'] = notification_kind
                data['notification_filter_label'] = {'all':'', 'schedule':'Изменения расписания',
                    'academic':'Учёба и сессия', 'announcements':'Объявления', 'deadlines':'Сроки объявлений',
                    'requests':'Статусы заявок'}[notification_kind]
                data['notification_only_requests'] = notification_kind == 'requests' or (
                    bool(events) and all(event.request_event_id is not None for event in events))
        elif actor.role == 'teacher':
            if section not in ('profile','notifications') or profile_id is not None:
                raise AccessDenied('forbidden',403)
            data.update(name=identity.instructor.name,faculty='',unread=0)
        elif section not in ('staff', 'profile', 'staff_requests', 'staff_request_detail', 'notifications'):
            raise AccessDenied('forbidden',403)
        else:
            if profile_id is not None:
                raise AccessDenied('not_found',404)
            scope = StaffScope.objects.select_related('faculty').get(identity=identity)
            data.update(name=identity.display_name,faculty=scope.faculty.name)
            if section in ('staff', 'staff_requests', 'staff_request_detail'):
                data.update(request_page(actor,request_id=request_id,status_filter=request_filter,page_number=request_page_number,queue_filters=request_queue_filters))
        if actor.role != 'student':
            from notifications.selectors import inbox_unread
            data['unread']=inbox_unread(actor,now)
            if section=='notifications':
                from workspaces.events import inbox
                data.update(inbox(actor,now,notification_kind,notification_page_number,notification_id,inbox_mode or ('history' if archive else 'inbox')))
        if extension:
            data.update(extension(actor, now))
    # Новый READ COMMITTED после materialized snapshot. Auth время намеренно свежее.
    with transaction.atomic():
        guard = lock_guard()
        current_actor(request,guard=guard,lock=True,expected=actor.marker)
    return data
