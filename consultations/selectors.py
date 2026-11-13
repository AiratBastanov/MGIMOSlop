"""Один доступ для списков, прямых ссылок, поиска, календаря и уведомлений."""
from datetime import timedelta
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Exists, F, OuterRef, Q

from accounts.access import AccessDenied
from accounts.models import StaffScope
from data_import.models import ImportRevision, AcademicCoverage
from materials.selectors import assignment_scope
from studies.academic_selectors import assessment_scope, enrollment_scope
from studies.selectors import schedule_snapshot, completeness_for, moscow
from student_requests.selectors import request_scope
from .models import ConsultationSlot, Booking, ConsultationEvent


SLOT_LABELS = {'open': 'Время открыто', 'closed': 'Запись закрыта', 'cancelled': 'Встреча отменена'}
BOOKING_LABELS = {'booked': 'Запись подтверждена', 'cancelled': 'Запись отменена',
                  'completed': 'Проведение отмечено принимающей стороной'}
EVENT_LABELS = {'created': 'Время открыто', 'closed': 'Запись закрыта', 'slot_cancelled': 'Встреча отменена принимающей стороной',
    'booked': 'Запись подтверждена', 'cancelled': 'Запись отменена', 'transferred': 'Запись перенесена на другое время',
    'venue': 'Место встречи уточнено', 'completed': 'Проведение отмечено принимающей стороной'}


def current_assignments(actor, now):
    valid = ImportRevision.objects.filter(dataset_id=OuterRef('course__dataset_id'),
        revision=F('dataset__current_revision'), valid_from__lte=now, valid_until__gt=now)
    return assignment_scope(actor, now).annotate(source_valid=Exists(valid)).filter(source_valid=True)


def _host_health(query):
    return query.filter(host__user__is_active=True, host__user__is_staff=False,
        host__user__is_superuser=False, host__user__groups__isnull=True,
        host__user__user_permissions__isnull=True, host__profile__isnull=True)


def slot_scope(actor, now):
    """Аудитория проверяется до временных фильтров, счёта и подробностей."""
    query = _host_health(ConsultationSlot.objects.all())
    if actor.role == 'student':
        faculty = actor.identity.profile.group.programme.faculty_id
        valid_staff = StaffScope.objects.filter(identity_id=OuterRef('host_id'), faculty_id=OuterRef('faculty_id'))
        query = query.annotate(staff_allowed=Exists(valid_staff))
        return query.filter(
            Q(kind='teacher', assignment__in=current_assignments(actor, now),
              host_id=F('assignment__teacher_id'), host__role='teacher', host__staffscope__isnull=True) |
            Q(kind='staff', faculty_id=faculty, staff_allowed=True, host__role='staff', host__instructor__isnull=True))
    if actor.role == 'teacher':
        return query.filter(host=actor.identity, kind='teacher', assignment__in=current_assignments(actor, now))
    if actor.role == 'staff':
        return query.filter(host=actor.identity, kind='staff', faculty_id__in=
                            StaffScope.objects.filter(identity=actor.identity).values('faculty_id'))
    raise AccessDenied('forbidden', 403)


def booking_scope(actor, now):
    if actor.role == 'student':
        # Смена аудитории не стирает личную историю и возможность отмены.
        return Booking.objects.filter(student_id=actor.profile_id)
    return Booking.objects.filter(slot__in=slot_scope(actor, now))


def available_slots(actor, now):
    occupied = Booking.objects.filter(slot_id=OuterRef('pk'), state='booked')
    return slot_scope(actor, now).filter(state='open', starts_at__gt=now).annotate(
        occupied=Exists(occupied)).filter(occupied=False)


def event_scope(actor, now):
    return ConsultationEvent.objects.filter(
        Q(booking__in=booking_scope(actor, now)) |
        (Q(slot__in=slot_scope(actor, now)) if actor.role != 'student' else Q(pk__in=[])))


def conflict_state(actor, now, starts_at, ends_at, *, exclude_booking=None, snapshot=None):
    if actor.role != 'student':
        return {'blocking': False, 'academic': False, 'complete': False,
                'message': 'Пересечения с учебным расписанием проверяет студент при записи.'}
    own = Booking.objects.filter(student_id=actor.profile_id, state='booked',
        starts_at__lt=ends_at, ends_at__gt=starts_at)
    if exclude_booking:
        own = own.exclude(pk=exclude_booking)
    blocking = own.exists()
    snapshot = snapshot or schedule_snapshot(actor, now)
    known_lessons = any(row['status'] == 'active' and row['starts_at'] < ends_at and starts_at < row['ends_at']
                        for row in snapshot['lessons'])
    known_assessment = assessment_scope(actor).filter(status='active', starts_at__lt=ends_at, ends_at__gt=starts_at).exists()
    academic = known_lessons or known_assessment
    first, last = moscow(starts_at).date(), moscow(ends_at - timedelta(microseconds=1)).date()
    dates = [first + timedelta(days=n) for n in range((last - first).days + 1)]
    source = snapshot['source']
    complete = (source['state'] == 'ready' and source['valid_from'] <= starts_at and ends_at <= source['valid_until']
                and all(completeness_for(snapshot, day) == 'complete' for day in dates))
    relevant = enrollment_scope(actor).filter(period__date_from__lte=last, period__date_until__gt=first).values_list('period_id', flat=True).distinct()
    coverage = AcademicCoverage.objects.filter(student_id=actor.profile_id, entity_type='assessment', completeness='complete',
        revision__dataset_id=actor.identity.profile.dataset_id, revision__revision=F('revision__dataset__current_revision')).values_list('period_id', flat=True)
    complete = complete and set(relevant).issubset(set(coverage))
    if blocking:
        message = 'Это время пересекается с другой подтверждённой записью. Выберите другое время или перенос.'
    elif academic:
        message = 'Есть пересечение с известным учебным событием. Подтвердите осознанный выбор этого времени.'
    elif complete:
        message = 'В подтверждённом расписании и ваших записях пересечений нет.'
    else:
        message = 'В известных данных пересечений нет; расписание подтверждено не полностью.'
    if academic and not complete:
        message += ' Расписание подтверждено не полностью.'
    return {'blocking': blocking, 'academic': academic, 'complete': bool(complete), 'message': message}


def slot_values(actor, row, now, *, snapshot=None, exclude_booking=None):
    host = row.host.instructor.name if row.kind == 'teacher' else row.host.display_name
    audience = f'{row.assignment.course.name} · {row.assignment.group.name}' if row.assignment_id else row.faculty.name
    occupied = getattr(row, 'occupied', None)
    if occupied is None:
        occupied = row.bookings.filter(state='booked').exists()
    host_allowed = actor.role != 'student' and row.host_id == actor.identity.pk and slot_scope(actor, now).filter(pk=row.pk).exists()
    return {'id': row.pk, 'version': row.version, 'kind': row.kind, 'topic': row.topic,
        'starts_at': moscow(row.starts_at).isoformat(), 'ends_at': moscow(row.ends_at).isoformat(),
        'time_label': f'{moscow(row.starts_at):%d.%m.%Y %H:%M}–{moscow(row.ends_at):%H:%M} МСК',
        'host': host, 'venue': row.venue, 'meeting_url': row.meeting_url, 'audience': audience,
        'state': row.state, 'state_label': SLOT_LABELS[row.state],
        'available': row.state == 'open' and row.starts_at > now and not occupied,
        'conflict': conflict_state(actor, now, row.starts_at, row.ends_at, exclude_booking=exclude_booking, snapshot=snapshot),
        'can_close': bool(host_allowed and row.state == 'open' and not occupied),
        'can_cancel': bool(host_allowed and row.state == 'open'),
        'can_venue': bool(host_allowed and row.state == 'open' and row.ends_at > now),
        'url': f'/consultations/?slot={row.pk}'}


def related_request(actor, request_id):
    if not request_id or actor.role == 'teacher':
        return None
    row = request_scope(actor).filter(pk=request_id).first()
    return {'id': row.pk, 'reference': row.reference,
            'url': f"/{'requests' if actor.role == 'student' else 'staff/requests'}/{row.pk}/"} if row else None


def booking_values(actor, row, now, *, snapshot=None, history=True):
    host = actor.role in ('teacher', 'staff')
    current_audience = slot_scope(actor, now).filter(pk=row.slot_id).exists()
    result = {'id': row.pk, 'version': row.version, 'state': row.state, 'state_label': BOOKING_LABELS[row.state],
        'slot': slot_values(actor, row.slot, now, snapshot=snapshot, exclude_booking=row.pk),
        'question': row.question, 'request': related_request(actor, row.request_id),
        'audience_current': current_audience,
        'audience_message': '' if current_audience else 'Новая запись в эту аудиторию недоступна. Ваша сохранённая запись и история остаются у вас.',
        'academic_ack': row.academic_ack,
        'can_cancel': row.state == 'booked' and row.ends_at > now,
        'can_rebook': not host and row.state == 'booked' and row.starts_at > now,
        'can_complete': host and row.state == 'booked' and row.ends_at <= now,
        'url': f'/consultations/bookings/{row.pk}/',
        'replaces_id': row.replaces_id,
        'history': [{'action': e.action, 'label': EVENT_LABELS[e.action], 'at': moscow(e.created_at).isoformat()}
                    for e in row.events.order_by('created_at', 'pk')] if history else []}
    if host:
        result['student'] = row.student.name
    return result


def _select(query):
    return query.select_related('host__instructor', 'assignment__course', 'assignment__group', 'faculty')


def _bookings(query):
    return query.select_related('student', 'request', 'slot__host__instructor', 'slot__assignment__course',
                                'slot__assignment__group', 'slot__faculty')


def _int(params, key, default=None):
    value = params.get(key, default)
    if value is None or value == '':
        return None
    if not str(value).isdigit() or not 1 <= int(value) <= 2147483647:
        raise AccessDenied('invalid_query', 400)
    return int(value)


def page_data(actor, now, params, booking_id=None):
    allowed = {'page', 'booking_page', 'assignment', 'course', 'kind', 'request', 'slot', 'rebook'}
    if set(params) - allowed or params.get('kind', '') not in ('', 'teacher', 'staff'):
        raise AccessDenied('invalid_query', 400)
    query = available_slots(actor, now) if actor.role == 'student' else slot_scope(actor, now)
    course = _int(params, 'course')
    if course:
        if actor.role != 'student' or not current_assignments(actor, now).filter(course_id=course).exists():
            raise AccessDenied('not_found', 404)
        query = query.filter(assignment__course_id=course)
    assignment = _int(params, 'assignment')
    if assignment:
        query = query.filter(assignment_id=assignment)
    if params.get('kind'):
        query = query.filter(kind=params['kind'])
    selected_slot = _int(params, 'slot')
    # Прямая ссылка проверяется тем же набором, не раскрывает закрытую чужую встречу.
    if selected_slot and not query.filter(pk=selected_slot).exists():
        selected_slot = None
    request_context = None
    if params.get('request'):
        request_context = related_request(actor, _int(params, 'request'))
        if not request_context:
            raise AccessDenied('not_found', 404)
    rebook = _int(params, 'rebook')
    if rebook and (actor.role != 'student' or not booking_scope(actor, now).filter(pk=rebook, state='booked').exists()):
        raise AccessDenied('not_found', 404)
    slots = Paginator(_select(query).order_by('starts_at', 'pk'), 12)
    selected = slots.get_page(_int(params, 'page', 1))
    slot_rows = list(selected)
    if selected_slot and selected_slot not in [row.pk for row in slot_rows]:
        slot_rows.insert(0, _select(query).get(pk=selected_slot))
    bookings = booking_scope(actor, now)
    if booking_id and not bookings.filter(pk=booking_id).exists():
        raise AccessDenied('not_found', 404)
    booking_pages = Paginator(_bookings(bookings).order_by('-starts_at', '-pk'), 12)
    booking_page = booking_pages.get_page(_int(params, 'booking_page', 1))
    rows = list(booking_page)
    if booking_id and booking_id not in [row.pk for row in rows]:
        rows.insert(0, _bookings(bookings).get(pk=booking_id))
    snapshot = schedule_snapshot(actor, now) if actor.role == 'student' else None
    audiences = []
    if actor.role == 'teacher':
        audiences = [{'id': a.pk, 'kind': 'teacher', 'label': f'{a.course.name} · {a.group.name}'}
                     for a in current_assignments(actor, now).select_related('course', 'group').order_by('pk')[:100]]
    elif actor.role == 'staff':
        audiences = [{'id': scope.faculty_id, 'kind': 'staff', 'label': scope.faculty.name}
                     for scope in StaffScope.objects.filter(identity=actor.identity).select_related('faculty').order_by('faculty_id')[:100]]
    clean_params = {k: v for k, v in params.items() if k in allowed}
    return {'marker': actor.marker, 'role': actor.role, 'endpoint': '/consultations/actions/',
        'state_url': (f'/consultations/bookings/{booking_id}/state/' if booking_id else '/consultations/state/') +
                     ('?' + urlencode(clean_params) if clean_params else ''),
        'slots': [slot_values(actor, row, now, snapshot=snapshot, exclude_booking=rebook) for row in slot_rows],
        'bookings': [booking_values(actor, row, now, snapshot=snapshot) for row in rows],
        'audiences': audiences, 'page': selected.number, 'pages': slots.num_pages, 'count': slots.count,
        'booking_page': booking_page.number, 'booking_pages': booking_pages.num_pages, 'booking_count': booking_pages.count,
        'selected_booking': booking_id, 'selected_slot': selected_slot, 'request_context': request_context,
        'now': moscow(now).isoformat(), 'preferred_assignment': assignment,
        'preferred_course': course,
        'source_message': snapshot['source']['message'] if snapshot else ''}


def calendar_events(actor, now, start=None, end=None):
    query = booking_scope(actor, now).filter(state='booked')
    if start:
        query = query.filter(ends_at__gt=start)
    if end:
        query = query.filter(starts_at__lt=end)
    snapshot = schedule_snapshot(actor, now) if actor.role == 'student' else None
    result = []
    for row in _bookings(query).order_by('starts_at', 'pk')[:100]:
        value = booking_values(actor, row, now, snapshot=snapshot, history=False)
        result.append({'id': row.pk, 'title': row.slot.topic, 'kind': 'consultation', 'url': value['url'],
            'starts_at': moscow(row.starts_at), 'ends_at': moscow(row.ends_at), 'date': moscow(row.starts_at).date(),
            'host': value['slot']['host'], 'venue': row.slot.venue, 'meeting_url': row.slot.meeting_url,
            'state_label': value['state_label'], 'conflict': value['slot']['conflict'],
            'time_label': f'{moscow(row.starts_at):%H:%M}–'+(f'{moscow(row.ends_at):%d.%m %H:%M}' if moscow(row.starts_at).date()!=moscow(row.ends_at).date() else f'{moscow(row.ends_at):%H:%M}')})
    return result


def student_upcoming(actor, now):
    return calendar_events(actor, now, start=now)[:3]


def discovery_records(actor, now, limit=200):
    limit = max(1, min(int(limit), 200))
    bookings = list(_bookings(booking_scope(actor, now)).order_by('-starts_at', '-pk')[:limit + 1])
    slots = list(_select(available_slots(actor, now) if actor.role == 'student' else slot_scope(actor, now))
                 .order_by('starts_at', 'pk')[:limit + 1])
    items = [{'id': f'booking:{r.pk}', 'title': r.slot.topic,
        'context': f'{BOOKING_LABELS[r.state]} · {moscow(r.starts_at):%d.%m.%Y %H:%M} МСК',
        'url': f'/consultations/bookings/{r.pk}/'} for r in bookings[:limit]]
    items += [{'id': f'slot:{r.pk}', 'title': r.topic,
        'context': f'Консультация · {moscow(r.starts_at):%d.%m.%Y %H:%M} МСК · {SLOT_LABELS[r.state]}',
        'url': f'/consultations/?slot={r.pk}'} for r in slots[:limit]]
    return {'items': items[:limit], 'partial': len(bookings) + len(slots) > limit}


def notification_values(notification, now):
    app = notification.application_event
    event = app.consultation_event
    payload = event.payload
    return {'id': notification.pk, 'kind': 'application', 'event_id': f'application:{app.pk}',
        'published_at': event.created_at, 'is_read': notification.is_read, 'available': True, 'can_read': True,
        'read_url': f'/notifications/{notification.pk}/read/', 'detail_url': f'/notifications/{notification.pk}/',
        'source_message': '', 'is_historical': bool(event.booking_id and event.booking_version != event.booking.version),
        'title': EVENT_LABELS[event.action], 'body': payload.get('topic', '') + ' · ' + payload.get('time_label', ''),
        'status_label': EVENT_LABELS[event.action], 'action_label': 'Открыть запись', 'history_url': '', 'deadline_label': '',
        'target_url': f'/consultations/bookings/{event.booking_id}/' if event.booking_id else f'/consultations/?slot={event.slot_id}'}
