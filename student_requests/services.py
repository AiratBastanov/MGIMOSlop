"""Guard → текущая сессия → заявка. Только эти операции пишут workflow."""
import hashlib
import json
import re
import unicodedata
import uuid

from django.core import signing
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from accounts.access import AccessDenied, current_actor, identity_for, lock_guard
from accounts.models import Identity, StaffScope
from .models import Request, RequestEvent
from .workflow import (REQUEST_TYPE, REQUEST_TYPES, TEXT_LIMIT, TRANSITIONS, PAYLOAD_FIELDS,
                       CERTIFICATE_PURPOSES, CORRECTION_AREAS)


class RequestError(Exception):
    def __init__(self, code, *, status=400, field_errors=None):
        self.code, self.status = code, status
        self.field_errors = field_errors or {}
        super().__init__(code)


def normalize_text(value, *, field='comment'):
    """NFC, единый LF, внешние Unicode-пробелы; без усечения текста."""
    if not isinstance(value, str):
        raise RequestError('invalid_input', field_errors={field: 'Введите обычный текст.'})
    return unicodedata.normalize('NFC', value.replace('\r\n', '\n').replace('\r', '\n')).strip()


def text_error(value, *, required=False):
    if required and not value:
        return 'Заполните это поле.'
    if len(value) > TEXT_LIMIT:
        return f'Сократите текст до {TEXT_LIMIT} символов. Сейчас: {len(value)}.'
    if any(unicodedata.category(char) in ('Cc', 'Cs') and char not in '\n\t' for char in value):
        return 'Удалите недопустимые управляющие символы.'
    return None


def fingerprint(request_type, comment):
    payload = json.dumps({'type': request_type, 'comment': comment}, ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8', errors='surrogatepass')).hexdigest()


def typed_fingerprint(request_type, comment, fields):
    payload = json.dumps({'schema': 2, 'type': request_type, 'comment': comment, 'fields': fields},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8', errors='surrogatepass')).hexdigest()


def issue_submission(actor, *, payload_schema=1):
    if actor.role != 'student':
        raise AccessDenied('forbidden', 403)
    if payload_schema not in (1, 2):
        raise ValueError('payload_schema')
    return signing.dumps({'key': str(uuid.uuid4()), 'user': actor.user_id, 'owner': actor.profile_id,
                          'payload_schema': payload_schema,
                          'context': str(actor.context_id), 'marker': actor.marker},
                         salt='student-request-submission-v1')


def _submission_data(token, actor):
    try:
        payload = signing.loads(token, salt='student-request-submission-v1')
        if (payload['user'] != actor.user_id or payload['owner'] != actor.profile_id
                or payload['context'] != str(actor.context_id)
                or not constant_time_compare(payload['marker'], actor.marker)):
            raise ValueError('context')
        schema = payload.get('payload_schema', 1)
        if type(schema) is not int or schema not in (1, 2):
            raise ValueError('schema')
        return uuid.UUID(payload['key']), schema
    except (signing.BadSignature, TypeError, ValueError, KeyError, AttributeError):
        raise AccessDenied('stale_context', 409)


def _submission_key(token, actor):
    return _submission_data(token, actor)[0]


def _require_role(actor, role):
    if actor.role != role:
        raise AccessDenied('forbidden', 403)


def _validate_payload(request_type, comment):
    errors = {}
    if request_type != REQUEST_TYPE:
        errors['request_type'] = 'Выберите справку об обучении.'
    error = text_error(comment)
    if error:
        errors['comment'] = error
    if errors:
        raise RequestError('invalid_input', field_errors=errors)


def normalize_fields(fields):
    if fields is None:
        return {}
    if not isinstance(fields, dict) or any(not isinstance(key, str) for key in fields):
        raise RequestError('invalid_input', field_errors={'request_type': 'Откройте форму выбранного типа заново.'})
    result = {key: normalize_text(value, field=key) for key, value in fields.items()}
    if 'copies' in result and re.fullmatch(r'[0-9]{1,6}', result['copies']):
        result['copies'] = int(result['copies'])
    return result


def validate_typed_payload(request_type, comment, fields):
    errors = {}
    if request_type not in PAYLOAD_FIELDS:
        errors['request_type'] = 'Выберите тип из каталога заявок.'
    error = text_error(comment)
    if error:
        errors['comment'] = error
    expected = PAYLOAD_FIELDS.get(request_type, ())
    if set(fields) - set(expected):
        errors['request_type'] = 'В форме есть поля другого типа заявки. Откройте нужную форму заново.'
    for name in expected:
        value = fields.get(name, '')
        if name == 'copies':
            if type(value) is not int or not 1 <= value <= 10:
                errors[name] = 'Укажите от 1 до 10 экземпляров.'
        elif name in ('purpose', 'correction_area'):
            choices = CERTIFICATE_PURPOSES if name == 'purpose' else CORRECTION_AREAS
            if value not in choices:
                errors[name] = 'Выберите один из предложенных вариантов.'
        else:
            error = text_error(value, required=True)
            if error:
                errors[name] = error
            elif name in ('recipient', 'record_label') and len(value) > 160:
                errors[name] = 'Сократите текст до 160 символов.'
    if errors:
        raise RequestError('invalid_input', field_errors=errors)


def applicant_values(profile):
    return {'name': profile.name, 'group': profile.group.name,
            'faculty': profile.group.programme.faculty.name,
            'programme': profile.group.programme.name, 'year': profile.year}


def _record_event(row, *, actor_id, action, from_status, explanation='', operator=''):
    # Notification обязательна: исключение в ней откатывает всю внешнюю atomic.
    from notifications.services import record_request_event
    event = RequestEvent.objects.create(request=row, actor_id=actor_id, recorded_at=row.updated_at,
        action=action, from_status=from_status, to_status=row.status, version=row.version,
        explanation=explanation, operator=operator)
    record_request_event(event)
    return event


def _create(*, profile, actor_id, key, request_type, comment, provenance='student', operator='',
            payload_schema=1, payload_data=None):
    payload_data = payload_data or {}
    if payload_schema == 1:
        _validate_payload(request_type, comment)
        accepted_fingerprint = fingerprint(request_type, comment)
        snapshot = {}
    elif payload_schema == 2:
        validate_typed_payload(request_type, comment, payload_data)
        accepted_fingerprint = typed_fingerprint(request_type, comment, payload_data)
        snapshot = applicant_values(profile)
    elif payload_schema == 3 and request_type == 'exchange_plan_review':
        from journeys.services import validate_snapshot
        validate_snapshot(payload_data)
        accepted_fingerprint = hashlib.sha256(json.dumps({'schema':3,'type':request_type,'comment':comment,'fields':payload_data},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        snapshot = applicant_values(profile)
    else:
        raise RequestError('invalid_input')
    now = timezone.now()
    row = Request.objects.create(owner=profile, servicing_faculty_id=profile.group.programme.faculty_id,
        submission_key=key, request_type=request_type, comment=comment,
        payload_schema=payload_schema, payload_data=payload_data, applicant_snapshot=snapshot,
        payload_fingerprint=accepted_fingerprint, status='submitted', version=1,
        created_at=now, updated_at=now, provenance=provenance)
    _record_event(row, actor_id=actor_id, action='submit', from_status='', operator=operator)
    return row


def create_journey_request(*, actor, key, snapshot=None, correction=None, comment=''):
    """Called inside the journey's guard/session/parent transaction after preview verification."""
    _require_role(actor, 'student')
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError('Journey submission requires an atomic operation')
    if snapshot is not None:
        return _create(profile=actor.identity.profile, actor_id=actor.user_id, key=key,
            request_type='exchange_plan_review', comment='', payload_schema=3, payload_data=snapshot)
    return _create(profile=actor.identity.profile, actor_id=actor.user_id, key=key,
        request_type='academic_correction', comment=comment, payload_schema=2, payload_data=correction)


def submit_request(request, marker, submission_token, request_type, comment, fields=None):
    with transaction.atomic():
        guard = lock_guard()
        actor = current_actor(request, guard=guard, lock=True, expected=marker)
        _require_role(actor, 'student')
        key, payload_schema = _submission_data(submission_token, actor)
        normalized = normalize_text(comment)
        if not isinstance(request_type, str):
            raise RequestError('invalid_input', field_errors={'request_type': 'Выберите тип из каталога заявок.'})
        payload_data = normalize_fields(fields)
        if payload_schema == 1 and payload_data:
            raise RequestError('invalid_input', field_errors={'request_type': 'Эта старая форма не поддерживает новые поля. Откройте новую заявку.'})
        accepted_fingerprint = (fingerprint(request_type, normalized) if payload_schema == 1 else
                                typed_fingerprint(request_type, normalized, payload_data))
        row = Request.objects.select_for_update().filter(owner_id=actor.profile_id, submission_key=key).first()
        # После возможного ожидания target lock срок и контекст проверяются снова,
        # в том числе до возврата уже принятого idempotent результата.
        actor = current_actor(request, guard=guard, lock=True, expected=marker)
        _require_role(actor, 'student')
        _submission_key(submission_token, actor)
        if row:
            if row.payload_schema != payload_schema or row.payload_fingerprint != accepted_fingerprint:
                raise RequestError('payload_conflict', status=409)
            return {'id': row.pk, 'marker': actor.marker, 'created': False}
        row = _create(profile=actor.identity.profile, actor_id=actor.user_id, key=key,
                      request_type=request_type, comment=normalized, payload_schema=payload_schema,
                      payload_data=payload_data)
        return {'id': row.pk, 'marker': actor.marker, 'created': True}


def _version(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,9}', str(value)):
        raise RequestError('invalid_input', field_errors={'expected_version': 'Откройте актуальную страницу заявки.'})
    return int(value)


def _validate_transition(row, action, expected_version, explanation):
    if row.version != _version(expected_version):
        raise RequestError('version_conflict', status=409)
    transition = TRANSITIONS.get(action) if isinstance(action, str) else None
    if transition is None or row.status not in transition.from_states:
        raise RequestError('transition_conflict', status=409)
    normalized = normalize_text(explanation, field='explanation')
    error = text_error(normalized, required=bool(transition.explanation_label))
    if error:
        raise RequestError('invalid_input', field_errors={'explanation': error})
    if not transition.explanation_label and normalized:
        raise RequestError('invalid_input', field_errors={'explanation': 'Для взятия в работу пояснение не требуется.'})
    return transition, normalized


def _transition(row, *, actor_id, action, expected_version, explanation='', operator=''):
    transition, explanation = _validate_transition(row, action, expected_version, explanation)
    from workspaces.faculty_services import terminal_clarifications
    terminal_clarifications(row,action,actor_id)
    before = row.status
    row.status = transition.to_status
    row.version += 1
    row.updated_at = timezone.now()
    row.save(update_fields=['status', 'version', 'updated_at'])
    _record_event(row, actor_id=actor_id, action=action, from_status=before,
                  explanation=explanation, operator=operator)
    return row


def transition_request(request, id, marker, action, expected_version, explanation=''):
    from .selectors import request_scope
    with transaction.atomic():
        guard = lock_guard()
        actor = current_actor(request, guard=guard, lock=True, expected=marker)
        _require_role(actor, 'staff')
        row = request_scope(actor).select_for_update(of=('self',)).filter(pk=id).first()
        if row is None:
            raise AccessDenied('not_found', 404)
        actor = current_actor(request, guard=guard, lock=True, expected=marker)
        _require_role(actor, 'staff')
        if not request_scope(actor).filter(pk=row.pk).exists():
            raise AccessDenied('not_found', 404)
        _transition(row, actor_id=actor.user_id, action=action, expected_version=expected_version,
                    explanation=explanation)
        return {'id': row.pk, 'marker': actor.marker}


SEED_KEYS = {
    'student_a': uuid.UUID('1ed1825c-85ba-50a6-8cb6-2d0c0feee2e1'),
    'student_b': uuid.UUID('c35e9773-6aa6-5625-985e-3e6b8d724ac5'),
}


def bootstrap_examples(*, operator):
    """Явный одноразовый bootstrap; существующая запись никогда не меняется.

    Оператор не изображает историческую web-сессию: provenance/operator и
    настоящее recorded_at отмечают синтетический пример. Доменный путь общий.
    """
    if not isinstance(operator, str) or not operator.strip() or len(operator) > 100:
        raise ValueError('Укажите имя оператора до 100 символов.')
    result = []
    with transaction.atomic():
        lock_guard()
        staff_identity = Identity.objects.select_related('user').get(user__username='staff_demo')
        _, staff_identity = identity_for(staff_identity.user_id)
        if staff_identity.role != 'staff':
            raise ValueError('Сотрудник демонстрации не имеет допустимого назначения.')
        scope = StaffScope.objects.get(identity=staff_identity)
        for username, key in SEED_KEYS.items():
            identity = Identity.objects.get(user__username=username)
            _, identity = identity_for(identity.user_id)
            if identity.role != 'student' or identity.profile.group.programme.faculty_id != scope.faculty_id:
                raise ValueError('Пример вне области обслуживания сотрудника.')
            existing = Request.objects.select_for_update().filter(owner=identity.profile, submission_key=key).first()
            if existing:
                result.append({'reference': existing.reference, 'created': False})
                continue
            row = _create(profile=identity.profile, actor_id=identity.user_id, key=key,
                request_type=REQUEST_TYPE, comment='Синтетический пример для демонстрации обработки заявки.',
                provenance='bootstrap', operator=operator)
            _transition(row, actor_id=staff_identity.user_id, action='take', expected_version=1, operator=operator)
            if username == 'student_b':
                _transition(row, actor_id=staff_identity.user_id, action='complete', expected_version=2,
                    explanation='Демонстрационный пример обработан. Действительная справка не выпускается.', operator=operator)
            result.append({'reference': row.reference, 'created': True})
    return result
