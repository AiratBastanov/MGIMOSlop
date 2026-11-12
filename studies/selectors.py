"""Материализованные операторские и scoped студенческие представления расписания."""
from datetime import datetime, time, timedelta
from django.utils import timezone
from data_import.contracts import DATASET_ID, MOSCOW
from data_import.models import Dataset, ImportRevision, Coverage, ImportAttempt
from data_import.services import read_snapshot
from data_import.state import records, scalar, canonical_state
from .models import ScheduleChange, Lesson, Instructor, Period
from notifications.models import Notification

def operator_schedule(*, group_id, period_id, date):
    now = timezone.now()
    with read_snapshot():
        head = Dataset.objects.filter(pk=DATASET_ID).first()
        if not head or not head.current_revision:
            attempt = ImportAttempt.objects.order_by('-recorded_at','-id').first()
            return {'state':'not_prepared','revision':0,'lessons':[], 'rendered_at':scalar(now),
                    'coverage':'unknown','total':None,'source_as_of':None,
                    'update_status':attempt.outcome if attempt else 'not_attempted'}
        revision = ImportRevision.objects.get(dataset=head,revision=head.current_revision)
        coverage = Coverage.objects.filter(revision=revision,group__source_id=group_id,
            period__source_id=period_id,date_from__lte=date,date_until__gt=date).first()
        current = records()
        lessons = sorted([row for row in current['lesson'].values() if row['group_id'] == group_id and row['period_id'] == period_id and datetime.fromisoformat(row['starts_at']).astimezone(MOSCOW).date() == date and row['status'] == 'active'], key=lambda row:(row['starts_at'],row['source_id']))
        for row in lessons:
            row['course_name'] = current['course'][row['course_id']]['name']
            row['instructor_name'] = current['instructor'][row['instructor_id']]['name'] if row['instructor_id'] else None
            row['starts_at_moscow'] = datetime.fromisoformat(row['starts_at']).astimezone(MOSCOW).isoformat()
            row['ends_at_moscow'] = datetime.fromisoformat(row['ends_at']).astimezone(MOSCOW).isoformat()
        completeness = coverage.completeness if coverage else 'unknown'
        applicable = revision.valid_from <= now < revision.valid_until
        state = 'expired' if not applicable else (('complete' if lessons else 'complete_empty') if completeness == 'complete' else completeness)
        history = list(ScheduleChange.objects.filter(dataset=head,lesson__group__source_id=group_id,lesson__period__source_id=period_id).order_by('recorded_at','event_id').values('event_id','before','after','reason','revision__revision'))
        notifications = list(Notification.objects.filter(group__source_id=group_id,schedule_change__lesson__period__source_id=period_id).order_by('schedule_change__event_id').values_list('schedule_change__event_id',flat=True))
        attempt = ImportAttempt.objects.filter(recorded_at__gte=revision.imported_at).order_by('-recorded_at','-id').first()
        return {'state':state,'revision':head.current_revision,'coverage':completeness,
                'source_as_of':scalar(revision.source_as_of),'imported_at':scalar(revision.imported_at),
                'valid_from':scalar(revision.valid_from),'valid_until':scalar(revision.valid_until),
                'rendered_at':scalar(now),'lessons':lessons,'history':history,'notification_events':notifications,
                'update_status':attempt.outcome if attempt else 'accepted',
                'total':len(lessons) if applicable and completeness == 'complete' else None}

def operator_snapshot():
    with read_snapshot():
        head = Dataset.objects.filter(pk=DATASET_ID).first()
        return {'head': {'revision':head.current_revision,'state_hash':head.state_hash} if head else None,
                'canonical':canonical_state()}


WEEKDAYS = ('Понедельник','Вторник','Среда','Четверг','Пятница','Суббота','Воскресенье')
CHANGE_TITLES = {'correction':'Изменение аудитории','move':'Занятие перенесено',
                 'cancel':'Занятие отменено','substitute':'Замена преподавателя'}


def moscow(value):
    if isinstance(value,str):
        value = datetime.fromisoformat(value)
    return value.astimezone(MOSCOW)


def clock_label(value):
    """Не округлять переданные источником секунды и доли секунды."""
    value = moscow(value)
    if value.microsecond:
        return value.strftime('%H:%M:%S.%f').rstrip('0')
    return value.strftime('%H:%M:%S') if value.second else value.strftime('%H:%M')


def exact_moscow(value):
    value = moscow(value)
    return f'{value:%d.%m.%Y}, {clock_label(value)} МСК'


def lesson_values(lesson):
    """Единственная проекция текущего Lesson для дня, Today, detail и событий."""
    starts,ends = moscow(lesson.starts_at),moscow(lesson.ends_at)
    return {'id':lesson.pk,'source_id':lesson.source_id,'course_name':lesson.course.name,
            'starts_at':starts,'ends_at':ends,'date':starts.date(),
            'time_label':f'{clock_label(starts)}–{clock_label(ends)}',
            'room':lesson.room,'room_label':f'Аудитория {lesson.room}' if lesson.room else 'Аудитория не указана',
            'instructor_name':lesson.instructor.name if lesson.instructor_id else 'Преподаватель не указан',
            'status':lesson.status,'status_label':'Отменено' if lesson.status == 'cancelled' else 'По расписанию',
            'url':f'/lessons/{lesson.pk}/','period_id':lesson.period_id,
            'record_revision':lesson.record_revision}


def history_values(change, current, instructors):
    def occurrence(record):
        starts,ends = moscow(record['starts_at']),moscow(record['ends_at'])
        room = record.get('room')
        return {'source_id':record['source_id'],'course_name':current['course_name'],
                'starts_at':starts,'ends_at':ends,'date':starts.date(),
                'time_label':f'{clock_label(starts)}–{clock_label(ends)}',
                'room':room,'room_label':f'Аудитория {room}' if room else 'Аудитория не указана',
                'instructor_name':instructors.get(record.get('instructor_id'),'Преподаватель не указан'),
                'status':record['status'],'status_label':'Отменено' if record['status'] == 'cancelled' else 'По расписанию'}
    title = CHANGE_TITLES.get(change.change_type,'Изменение занятия')
    if change.change_type == 'correction' and change.before.get('room') == change.after.get('room'):
        title = 'Уточнение занятия'
    return {'event_id':change.event_id,'change_type':change.change_type,'title':title,
            'reason':change.reason,'before':occurrence(change.before),'after':occurrence(change.after),
            'published_at':moscow(change.recorded_at),'source_updated_at':moscow(change.source_updated_at),
            'current':current,'url':current['url'],
            'is_historical':change.after['record_revision'] != current['record_revision']}


def schedule_snapshot(actor, now):
    """Вызывается только внутри private_snapshot; сначала scope, затем материализация.

    Это небольшой принятый набор, без отдельного кеша и без ORM в шаблоне.
    Покрытие текущей revision не заимствуется у иной группы или периода.
    """
    dataset_id = actor.identity.profile.dataset_id
    head = Dataset.objects.filter(pk=dataset_id).first()
    if not head or not head.current_revision:
        attempt = ImportAttempt.objects.order_by('-recorded_at','-id').first()
        return {'source':{'state':'not_prepared','announcements_state':'not_prepared','message':'Данные ещё не подготовлены.',
                'source_as_of':None,'imported_at':None,'coverage_from':None,'coverage_until':None,
                'coverage_last_date':None,
                'update_status':attempt.outcome if attempt else 'not_attempted',
                'update_message':'Источник недоступен; подготовленные сведения ещё не поступили.' if attempt and attempt.outcome == 'source_unavailable' else ''},
                'lessons':[],'history':[],'coverage':[],'periods':{},'now':now}
    revision = ImportRevision.objects.get(dataset=head,revision=head.current_revision)
    lessons = list(Lesson.objects.filter(dataset_id=dataset_id,group_id=actor.group_id)
        .select_related('course','instructor').order_by('starts_at','source_id'))
    current = {lesson.pk:lesson_values(lesson) for lesson in lessons}
    from profiles.selectors import person_for_instructor
    for lesson in lessons:
        current[lesson.pk]['instructor_person'] = person_for_instructor(actor,lesson.instructor_id,current[lesson.pk]['instructor_name'])
    changes = list(ScheduleChange.objects.filter(dataset_id=dataset_id,
        lesson__dataset_id=dataset_id,lesson__group_id=actor.group_id)
        .order_by('recorded_at','event_id'))
    referenced_instructors = {row.get('instructor_id') for change in changes for row in (change.before,change.after)}
    instructors = dict(Instructor.objects.filter(dataset_id=dataset_id,source_id__in=referenced_instructors)
        .values_list('source_id','name'))
    history = [history_values(change,current[change.lesson_id],instructors) for change in changes]
    coverage = list(Coverage.objects.filter(revision=revision,group_id=actor.group_id,
        group__dataset_id=dataset_id,period__dataset_id=dataset_id)
        .order_by('date_from','period_id').values('period_id','date_from','date_until','completeness'))
    period_ids = {row['period_id'] for row in coverage} | {row.period_id for row in lessons}
    periods = {row['id']:row for row in Period.objects.filter(dataset_id=dataset_id,pk__in=period_ids)
        .values('id','date_from','date_until')}
    attempt = ImportAttempt.objects.filter(recorded_at__gte=revision.imported_at).order_by('-recorded_at','-id').first()
    update_status = attempt.outcome if attempt else 'accepted'
    update_message = ''
    if update_status in ('rejected','invalid_update'):
        update_message = 'Обновление не применено. Показаны ранее принятые данные.'
    elif update_status in ('source_unavailable','unavailable'):
        update_message = 'Источник обновления недоступен. Показаны ранее принятые данные.'
    if now >= revision.valid_until:
        state,message = 'expired','Срок действия данных истёк. Расписание доступно только как архив.'
    elif now < revision.valid_from:
        state,message = 'not_valid','Период действия подготовленных данных ещё не начался.'
    elif not coverage:
        state,message = 'not_prepared','Для вашей группы расписание ещё не подготовлено.'
    else:
        state,message = 'ready',''
    announcements_state = 'not_prepared' if revision.schema_version < 3 else (
        state if state in ('expired','not_valid') else 'ready')
    return {'source':{'state':state,'announcements_state':announcements_state,'message':message,'source_as_of':moscow(revision.source_as_of),
            'imported_at':moscow(revision.imported_at),'valid_from':moscow(revision.valid_from),
            'valid_until':moscow(revision.valid_until),
            'coverage_from':min((x['date_from'] for x in coverage),default=None),
            'coverage_until':max((x['date_until'] for x in coverage),default=None),
            'coverage_last_date':max((x['date_until'] for x in coverage),default=now.date())-timedelta(days=1) if coverage else None,
            'update_status':update_status,'update_message':update_message},
            'lessons':list(current.values()),'history':history,'coverage':coverage,'periods':periods,'now':now}


def completeness_for(snapshot, date):
    relevant_periods = [pid for pid,period in snapshot['periods'].items()
                        if period['date_from'] <= date < period['date_until']]
    rows = [row for row in snapshot['coverage'] if row['date_from'] <= date < row['date_until']]
    if not rows:
        source = snapshot['source']
        if source['coverage_from'] is None:
            return 'not_prepared'
        if date < source['coverage_from'] or date >= source['coverage_until']:
            return 'outside'
        return 'unknown'
    if any(row['completeness'] == 'partial' for row in rows):
        return 'partial'
    if any(row['completeness'] == 'unknown' for row in rows) or any(
            pid not in {row['period_id'] for row in rows} for pid in relevant_periods):
        return 'unknown'
    return 'complete'


def schedule_day(snapshot, date):
    lessons = [row for row in snapshot['lessons'] if row['date'] == date and row['status'] == 'active']
    markers = []
    for change in snapshot['history']:
        before,after,current = change['before'],change['after'],change['current']
        if before['date'] == date and before['starts_at'] != after['starts_at'] and before['starts_at'] != current['starts_at']:
            markers.append(dict(change,marker_type='moved',marker_time=before['starts_at'],
                                marker_label='Прежнее время · занятие перенесено'))
    for row in snapshot['lessons']:
        if row['date'] == date and row['status'] == 'cancelled':
            markers.append({'marker_type':'cancelled','marker_time':row['starts_at'],
                            'marker_label':'Отменённое занятие','current':row,'before':row,
                            'after':row,'url':row['url'],'title':'Занятие отменено'})
    markers.sort(key=lambda row:(row['marker_time'],row['current']['source_id']))
    latest_changes = {}
    active_ids = {row['id'] for row in lessons}
    for change in snapshot['history']:
        if change['current']['id'] in active_ids and not change['is_historical']:
            latest_changes[change['current']['id']] = change
    completeness = completeness_for(snapshot,date)
    state = completeness
    if snapshot['source']['state'] in ('expired','not_valid','not_prepared'):
        state = snapshot['source']['state']
    messages = {'partial':'Расписание неполное. Показаны только известные занятия.',
                'unknown':'Нет подтверждённых данных на выбранную дату. Список может быть неполным.',
                'not_prepared':'Данные для вашей группы ещё не подготовлены.',
                'outside':'Эта дата за пределами подготовленного расписания.',
                'expired':'Срок действия данных истёк. Ниже — архив расписания.',
                'not_valid':'Период действия подготовленных данных ещё не начался.'}
    if state == 'complete' and not lessons:
        state,message = 'complete_empty','На эту дату занятий нет.'
    elif state == 'complete' and date == moscow(snapshot['now']).date() and all(row['ends_at'] <= snapshot['now'] for row in lessons):
        state,message = 'finished','Занятия на сегодня завершились.'
    else:
        message = messages.get(state,'')
    total = len(lessons) if state in ('complete','complete_empty','finished') else None
    return {'date':date,'date_iso':date.isoformat(),'weekday':WEEKDAYS[date.weekday()],
            'is_today':date == moscow(snapshot['now']).date(),'lessons':lessons,'markers':markers,'changes':list(latest_changes.values()),
            'state':state,'completeness':completeness,'message':message,'total':total,
            'count_label':f'Занятий: {total}' if total is not None else f'Показано {len(lessons)}',
            'url':f'/schedule/?date={date.isoformat()}&view=day'}


def next_lesson(snapshot):
    now = snapshot['now']
    source = snapshot['source']
    if source['state'] != 'ready':
        return {'kind':'none','label':'Расписание','lesson':None,'message':source['message']}
    def covered(row):
        return row['starts_at'] < source['valid_until'] and any(item['period_id'] == row['period_id'] and item['date_from'] <= row['date'] < item['date_until']
                   for item in snapshot['coverage'])
    active = [row for row in snapshot['lessons'] if row['status'] == 'active' and covered(row)]
    current = next((row for row in active if row['starts_at'] <= now < row['ends_at']),None)
    if current:
        return {'kind':'current','label':'Сейчас идёт','lesson':current,'message':''}
    future = next((row for row in active if row['starts_at'] > now),None)
    if future:
        date = moscow(now).date()
        complete = True
        while date <= future['date']:
            if completeness_for(snapshot,date) != 'complete':
                complete = False
                break
            date += timedelta(days=1)
        return {'kind':'next' if complete else 'known',
                'label':'Следующее занятие' if complete else 'Ближайшее известное занятие; расписание неполное',
                'lesson':future,'message':''}
    today = schedule_day(snapshot,moscow(now).date())
    message = 'Будущих занятий в подготовленном расписании нет.'
    coverage_until = source['coverage_until']
    date = moscow(now).date()
    complete = bool(coverage_until and date < coverage_until)
    while complete and date < coverage_until:
        complete = completeness_for(snapshot,date) == 'complete'
        date += timedelta(days=1)
    if not complete:
        message = 'Следующее занятие пока неизвестно: расписание неполное.'
    if today['state'] == 'finished':
        message = today['message'] + ' ' + message
    return {'kind':'none','label':'Ближайшее занятие','lesson':None,'message':message}
