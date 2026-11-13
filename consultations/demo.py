"""Явная синтетическая подготовка. Повтор не переносит встречи и не стирает действия."""
import json
import uuid
from datetime import timedelta
from pathlib import Path

from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction
from django.test import RequestFactory
from django.utils import timezone

from accounts.access import authenticate_session, current_actor, identity_for, lock_guard, logout_session
from accounts.models import Identity, StaffScope
from studies.selectors import moscow
from .models import ConsultationOperation, ConsultationSlot, Booking
from .selectors import current_assignments, conflict_state
from .services import perform

NAMESPACE = uuid.UUID('369436a0-657c-4f9a-a149-58a5b9282c46')
FINGERPRINT = 'connected-consultations-demo-v1'


def key(name):
    return str(uuid.uuid5(NAMESPACE, name))


def provision(credentials_path, *, operator):
    if not isinstance(operator, str) or not operator.strip() or len(operator) > 100:
        raise ValueError('Укажите оператора длиной до 100 символов.')
    with transaction.atomic():
        lock_guard()
        teacher = Identity.objects.get(user__username='teacher_demo')
        _, teacher = identity_for(teacher.user_id)
        old = ConsultationOperation.objects.filter(user_id=teacher.user_id, key=key('complete')).first()
        if old:
            if old.fingerprint != FINGERPRINT:
                raise ValueError('Конфликт версии демонстрационных консультаций.')
            ids = old.result.get('slots', [])
            if len(ids) != 4 or ConsultationSlot.objects.filter(pk__in=ids).count() != 4:
                raise ValueError('Демонстрационные записи не совпадают с сохранённой подготовкой.')
            return {'state': 'noop', **old.result}
        credentials = json.loads(Path(credentials_path).read_text(encoding='utf-8'))
        requests = {}
        for name, role in [('teacher_demo', 'teacher'), ('staff_demo', 'staff'), ('student_a', 'student')]:
            request = RequestFactory().post('/explicit-consultation-preparation/')
            request.session = SessionStore()
            if authenticate_session(request, name, credentials[name]) != role:
                raise ValueError('Не совпала роль демонстрационного аккаунта ' + name)
            requests[role] = request
        actors = {role: current_actor(request) for role, request in requests.items()}
        teacher, student, staff = actors['teacher'], actors['student'], actors['staff']
        assignment = current_assignments(teacher, teacher.checked_at).filter(
            pk__in=current_assignments(student, student.checked_at).values('pk')).order_by('pk').first()
        faculty = student.identity.profile.group.programme.faculty_id
        if not assignment or not StaffScope.objects.filter(identity=staff.identity, faculty_id=faculty).exists():
            raise ValueError('Нет общей действующей области преподавателя, студента и службы.')
        base = (moscow(timezone.now()) + timedelta(days=1)).replace(hour=18, minute=10, second=0, microsecond=0)
        # Выбираем один раз при явной инициализации; повторный запуск дату не двигает.
        for day in range(7):
            candidate = base + timedelta(days=day)
            conflict = conflict_state(student, student.checked_at, candidate, candidate + timedelta(hours=2))
            if not conflict['blocking'] and not conflict['academic']:
                base = candidate
                break
        slots = []
        for number, (role, topic, audience, offset) in enumerate([
            ('staff', 'План подготовки к вымышленному обмену', faculty, 30),
            ('staff', 'Вопрос по заявке факультетской службы', faculty, 60),
            ('teacher', 'Вопросы по учебной дисциплине', assignment.pk, 0),
            ('teacher', 'Подготовка учебных вопросов к обмену', assignment.pk, 90),
        ]):
            start = base + timedelta(minutes=offset)
            result = perform(requests[role], actors[role].marker, {'action': 'create', 'operation': key(f'slot-{number}'),
                'audience': str(audience), 'topic': topic, 'starts_at': start.isoformat(),
                'ends_at': (start + timedelta(minutes=20)).isoformat(),
                'venue': 'Демонстрационная аудитория 101' if role == 'teacher' else 'Демонстрационная приёмная 102',
                'meeting_url': ''})
            slots.append(result['slot_id'])
        chosen = ConsultationSlot.objects.get(pk=slots[2])
        result = perform(requests['student'], student.marker, {'action': 'book', 'operation': key('student-booking'),
            'slot': str(chosen.pk), 'version': str(chosen.version), 'question': 'Синтетический вопрос: как лучше подготовить учебный план?',
            'academic_ack': '1' if conflict_state(student, student.checked_at, chosen.starts_at, chosen.ends_at)['academic'] else ''})
        result = {'slots': slots, 'booking': result['booking_id'], 'initialized_at': timezone.now().isoformat()}
        ConsultationOperation.objects.create(user_id=teacher.user_id, key=key('complete'), fingerprint=FINGERPRINT, result=result)
        for role, request in requests.items():
            logout_session(request, current_actor(request).marker)
        return {'state': 'created', **result}
