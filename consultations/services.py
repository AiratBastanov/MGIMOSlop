"""Сохранение встречи: guard → session → owners → slots → booking → fresh access."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from accounts.access import AccessDenied, current_actor, lock_guard
from accounts.models import Identity, StaffScope
from data_import.contracts import MOSCOW
from materials.services import MaterialError, text, version
from studies.models import StudentProfile
from student_requests.selectors import request_scope
from workspaces.common import https_link, emit
from .models import ConsultationSlot, Booking, ConsultationEvent, ConsultationOperation
from .selectors import slot_scope, booking_scope, current_assignments, conflict_state, SLOT_LABELS, BOOKING_LABELS
from journeys.booking_links import owned_journey, associate


class BookingError(MaterialError):
    pass


def _id(data, field):
    raw = data.get(field, '')
    if not str(raw).isdigit() or not 1 <= int(raw) <= 2147483647:
        raise BookingError('Выберите доступную запись или время.', field=field)
    return int(raw)


def _instant(data, field):
    raw = data.get(field, '')
    if not isinstance(raw, str) or len(raw) > 40:
        raise BookingError('Укажите дату и время по Москве.', field=field)
    try:
        result = datetime.fromisoformat(raw)
        if timezone.is_naive(result):
            result = result.replace(tzinfo=MOSCOW)
        if result.second or result.microsecond:
            raise ValueError()
        return result
    except (ValueError, TypeError, OverflowError):
        raise BookingError('Укажите дату и время с точностью до минуты по Москве.', field=field)


def _operation(actor, data):
    try:
        key = uuid.UUID(str(data.get('operation', '')))
    except (ValueError, TypeError):
        raise BookingError('Не найден ключ операции. Откройте актуальную форму.', status=409)
    values = {k: str(data[k]) for k in data if k not in ('operation', 'context', 'csrfmiddlewaretoken')}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    old = ConsultationOperation.objects.filter(user_id=actor.user_id, key=key).first()
    if old and old.fingerprint != digest:
        raise BookingError('Этот ключ уже использован для другого действия. Проверьте сохранённый результат.', status=409)
    return key, digest, old.result if old else None


def _receipt_authority(actor, result, now):
    if result.get('booking_id'):
        allowed = booking_scope(actor, now).filter(pk=result['booking_id']).exists()
    else:
        allowed = slot_scope(actor, now).filter(pk=result.get('slot_id')).exists()
    if not allowed:
        raise AccessDenied('not_found', 404)
    if result.get('journey_id'):
        journey = owned_journey(actor, result['journey_id'])
        if not journey.booking_links.filter(booking_id=result['booking_id']).exists():
            raise AccessDenied('not_found', 404)


def _record(actor, key, digest, *, booking=None, slot=None, journey=None):
    result = {'state': 'saved'}
    if booking:
        result.update(booking_id=booking.pk, slot_id=booking.slot_id,
                      url=f'/consultations/bookings/{booking.pk}/')
    else:
        result.update(slot_id=slot.pk, url=f'/consultations/?slot={slot.pk}')
    if journey:
        result.update(journey_id=journey.pk, journey_url=f'/journeys/{journey.pk}/')
    ConsultationOperation.objects.create(user_id=actor.user_id, key=key, fingerprint=digest, result=result)
    return {**result, 'marker': actor.marker}


def _event(actor, slot, action, booking=None):
    payload = {'topic': slot.topic, 'starts_at': slot.starts_at.isoformat(), 'ends_at': slot.ends_at.isoformat(),
        'time_label': f'{slot.starts_at.astimezone(MOSCOW):%d.%m.%Y %H:%M}–'+(f'{slot.ends_at.astimezone(MOSCOW):%d.%m.%Y %H:%M}' if slot.starts_at.astimezone(MOSCOW).date()!=slot.ends_at.astimezone(MOSCOW).date() else f'{slot.ends_at.astimezone(MOSCOW):%H:%M}')+' МСК',
        'venue': slot.venue, 'meeting_url': slot.meeting_url, 'slot_state': slot.state,
        'booking_state': booking.state if booking else None}
    event = ConsultationEvent.objects.create(slot=slot, booking=booking, actor_id=actor.user_id,
        action=action, slot_version=slot.version, booking_version=booking.version if booking else None, payload=payload)
    emit('consultation', consultation_event=event)
    return event


def _lock_parents(host_ids, student_id=None):
    list(Identity.objects.select_for_update().filter(pk__in=host_ids).order_by('pk'))
    if student_id:
        StudentProfile.objects.select_for_update().get(pk=student_id)


def _check_version(actual, data, field='version'):
    if version(data.get(field, '')) != actual:
        raise BookingError('Запись изменилась. Обновите список и проверьте актуальное время.', field=field, status=409)


def _venue(data):
    venue = text(data.get('venue', ''), field='venue', limit=240)
    link = https_link(data.get('meeting_url', ''), field='meeting_url')
    if not venue and not link:
        raise BookingError('Укажите место встречи или явно настроенную HTTPS-ссылку.', field='venue')
    return venue, link


def _create(request, marker, guard, actor, data, key, digest):
    if actor.role not in ('teacher', 'staff'):
        raise AccessDenied('forbidden', 403)
    _lock_parents([actor.identity.pk])
    actor = current_actor(request, guard=guard, lock=True, expected=marker)
    audience = _id(data, 'audience')
    assignment = faculty_id = None
    if actor.role == 'teacher':
        assignment = current_assignments(actor, actor.checked_at).select_for_update(of=('self',)).filter(pk=audience).first()
        if not assignment:
            raise AccessDenied('not_found', 404)
    elif StaffScope.objects.filter(identity=actor.identity, faculty_id=audience).exists():
        faculty_id = audience
    else:
        raise AccessDenied('not_found', 404)
    starts, ends = _instant(data, 'starts_at'), _instant(data, 'ends_at')
    now = actor.checked_at
    if starts <= now or starts > now + timedelta(days=180):
        raise BookingError('Выберите будущее время в пределах 180 дней.', field='starts_at')
    if not timedelta(minutes=5) <= ends - starts <= timedelta(hours=4):
        raise BookingError('Длительность встречи — от 5 минут до 4 часов.', field='ends_at')
    if assignment and (starts < assignment.available_from or ends > assignment.available_until):
        raise BookingError('Встреча должна целиком попадать в действующее назначение преподавателя.', field='ends_at')
    if ConsultationSlot.objects.filter(host=actor.identity, state='open', ends_at__gt=now).count() >= 40:
        raise BookingError('У вас уже 40 открытых будущих встреч. Закройте неиспользуемые времена.', status=409)
    if ConsultationSlot.objects.filter(host=actor.identity, state='open', starts_at__lt=ends, ends_at__gt=starts).exists():
        raise BookingError('У вас уже открыта встреча, пересекающаяся с этим временем.', field='starts_at', status=409)
    venue, link = _venue(data)
    row = ConsultationSlot.objects.create(host=actor.identity, kind=actor.role, assignment=assignment, faculty_id=faculty_id,
        starts_at=starts, ends_at=ends, topic=text(data.get('topic', ''), field='topic', limit=160, required=True),
        venue=venue, meeting_url=link)
    _event(actor, row, 'created')
    return _record(actor, key, digest, slot=row)


def _host_action(request, marker, guard, actor, data, key, digest):
    if actor.role not in ('teacher', 'staff'):
        raise AccessDenied('forbidden', 403)
    slot_id = _id(data, 'slot')
    parent = slot_scope(actor, actor.checked_at).filter(pk=slot_id).first()
    if not parent:
        raise AccessDenied('not_found', 404)
    _lock_parents([parent.host_id])
    row = ConsultationSlot.objects.select_for_update().get(pk=parent.pk)
    actor = current_actor(request, guard=guard, lock=True, expected=marker)
    if not slot_scope(actor, actor.checked_at).filter(pk=row.pk).exists():
        raise AccessDenied('not_found', 404)
    _check_version(row.version, data)
    if row.state != 'open':
        raise BookingError('Это время уже закрыто или отменено.', status=409)
    booked = Booking.objects.select_for_update().filter(slot=row, state='booked').first()
    action = data['action']
    if action == 'close':
        if booked:
            raise BookingError('Есть подтверждённая запись. Используйте отмену встречи с уведомлением участника.', status=409)
        row.state = 'closed'
        event_action = 'closed'
    elif action == 'cancel_slot':
        row.state = 'cancelled'
        event_action = 'slot_cancelled'
        if booked:
            booked.state = 'cancelled'
            booked.version += 1
            booked.save()
    else:
        if row.ends_at <= actor.checked_at:
            raise BookingError('Время встречи прошло. История места сохраняется.', status=409)
        row.venue, row.meeting_url = _venue(data)
        event_action = 'venue'
    row.version += 1
    row.save()
    _event(actor, row, event_action, booked)
    return _record(actor, key, digest, slot=row)


def _shared_request(actor, slot, data):
    raw = data.get('request', '')
    if raw in ('', None):
        return None
    if slot.kind != 'staff':
        raise BookingError('Заявку факультетской службы нельзя передать преподавателю.', field='request')
    request_id = _id(data, 'request')
    row = request_scope(actor).filter(pk=request_id, servicing_faculty_id=slot.faculty_id).first()
    if not row or not StaffScope.objects.filter(identity_id=slot.host_id, faculty_id=row.servicing_faculty_id).exists():
        raise AccessDenied('not_found', 404)
    return row


def _book(request, marker, guard, actor, data, key, digest):
    if actor.role != 'student':
        raise AccessDenied('forbidden', 403)
    target = slot_scope(actor, actor.checked_at).filter(pk=_id(data, 'slot')).first()
    if not target:
        raise AccessDenied('not_found', 404)
    old = None
    if data['action'] == 'rebook':
        old = booking_scope(actor, actor.checked_at).select_related('slot').filter(pk=_id(data, 'booking')).first()
        if not old:
            raise AccessDenied('not_found', 404)
        if old.slot_id == target.pk:
            raise BookingError('Для переноса выберите другое время.', status=409)
    _lock_parents([target.host_id] + ([old.slot.host_id] if old else []), actor.profile_id)
    slots = {row.pk: row for row in ConsultationSlot.objects.select_for_update()
             .filter(pk__in=[target.pk] + ([old.slot_id] if old else [])).order_by('pk')}
    target = slots[target.pk]
    if old:
        old = Booking.objects.select_for_update().get(pk=old.pk)
    actor = current_actor(request, guard=guard, lock=True, expected=marker)
    now = actor.checked_at
    if not slot_scope(actor, now).filter(pk=target.pk).exists():
        raise AccessDenied('not_found', 404)
    _check_version(target.version, data, 'slot_version' if old else 'version')
    if old:
        _check_version(old.version, data)
        if old.student_id != actor.profile_id:
            raise AccessDenied('not_found', 404)
        if old.state != 'booked' or old.starts_at <= now:
            raise BookingError('Перенос доступен для вашей будущей подтверждённой записи.', status=409)
    if target.state != 'open' or target.starts_at <= now or Booking.objects.filter(slot=target, state='booked').exists():
        raise BookingError('Это время уже недоступно. Обновите список и выберите другое.', status=409)
    conflicts = conflict_state(actor, now, target.starts_at, target.ends_at, exclude_booking=old.pk if old else None)
    if conflicts['blocking']:
        raise BookingError(conflicts['message'], status=409)
    ack = data.get('academic_ack', '') == '1'
    if conflicts['academic'] and not ack:
        raise BookingError(conflicts['message'], field='academic_ack', status=409)
    question = text(data.get('question', ''), field='question', limit=1000)
    shared = _shared_request(actor, target, data)
    journey = owned_journey(actor, data['journey']) if 'journey' in data else None
    # Отмена старой и получение новой выполняются одной транзакцией. Любой
    # конфликт/ошибка возвращает старую запись полностью вместе с её событиями.
    if old:
        old.state = 'cancelled'
        old.version += 1
        old.save()
        _event(actor, slots[old.slot_id], 'transferred', old)
    row = Booking.objects.create(slot=target, student_id=actor.profile_id, starts_at=target.starts_at,
        ends_at=target.ends_at, question=question, request=shared, academic_ack=ack, replaces=old)
    if journey:
        associate(actor, journey, row)
    _event(actor, target, 'booked', row)
    return _record(actor, key, digest, booking=row, journey=journey)


def _booking_action(request, marker, guard, actor, data, key, digest):
    item = booking_scope(actor, actor.checked_at).select_related('slot').filter(pk=_id(data, 'booking')).first()
    if not item:
        raise AccessDenied('not_found', 404)
    _lock_parents([item.slot.host_id], item.student_id)
    slot = ConsultationSlot.objects.select_for_update().get(pk=item.slot_id)
    row = Booking.objects.select_for_update().get(pk=item.pk)
    actor = current_actor(request, guard=guard, lock=True, expected=marker)
    if not booking_scope(actor, actor.checked_at).filter(pk=row.pk).exists():
        raise AccessDenied('not_found', 404)
    _check_version(row.version, data)
    if row.state != 'booked':
        raise BookingError('Запись уже отменена или её проведение отмечено.', status=409)
    action = data['action']
    if action == 'complete':
        if actor.role not in ('teacher', 'staff'):
            raise AccessDenied('forbidden', 403)
        if row.ends_at > actor.checked_at:
            raise BookingError('Проведение можно отметить после окончания времени встречи.', status=409)
        row.state = 'completed'
        event_action = 'completed'
    else:
        if row.ends_at <= actor.checked_at:
            raise BookingError('Время встречи уже прошло. Запись сохраняется без автоматического подтверждения посещения.', status=409)
        row.state = 'cancelled'
        event_action = 'cancelled'
    row.version += 1
    row.save()
    _event(actor, slot, event_action, row)
    return _record(actor, key, digest, booking=row)


def perform(request, marker, data):
    allowed = {'action', 'operation', 'context', 'csrfmiddlewaretoken', 'version', 'slot_version', 'slot', 'booking',
               'audience', 'starts_at', 'ends_at', 'topic', 'venue', 'meeting_url', 'question', 'request', 'academic_ack', 'journey'}
    try:
        with transaction.atomic():
            guard = lock_guard()
            actor = current_actor(request, guard=guard, lock=True, expected=marker)
            if set(data) - allowed:
                raise BookingError('Форма содержит неподдерживаемые поля.')
            action = data.get('action', '')
            if action not in ('create', 'close', 'cancel_slot', 'venue', 'book', 'rebook', 'cancel', 'complete'):
                raise BookingError('Выберите доступное действие.')
            if 'journey' in data and action not in ('book', 'rebook'):
                raise BookingError('Подготовка связывается при подтверждении записи.')
            key, digest, result = _operation(actor, data)
            if result:
                # Проверяется исход операции и действующее право, а не время,
                # уже наступившее после её успешной фиксации.
                _receipt_authority(actor, result, actor.checked_at)
                return {**result, 'marker': actor.marker}
            if action == 'create':
                return _create(request, marker, guard, actor, data, key, digest)
            if action in ('close', 'cancel_slot', 'venue'):
                return _host_action(request, marker, guard, actor, data, key, digest)
            if action in ('book', 'rebook'):
                return _book(request, marker, guard, actor, data, key, digest)
            return _booking_action(request, marker, guard, actor, data, key, digest)
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.__cause__, 'diag', None), 'constraint_name', '')
        if constraint in ('consult_slot_capacity_one', 'consult_host_overlap', 'consult_student_overlap'):
            raise BookingError('Время уже занято или пересекается с другой записью. Сохранённая прежняя запись не изменена.', status=409) from exc
        raise
