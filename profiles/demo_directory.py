"""Явная синтетическая группа: учебные профили только через обычный импорт.

Существующие identities, фото и публикации никогда не восстанавливаются поверх
изменений владельца. Новые classmates имеют непригодный для входа пароль.
"""
from datetime import datetime, time
from hashlib import sha256
from io import BytesIO
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageDraw

from accounts.access import lock_guard
from accounts.models import AccessAudit, Identity, StaffScope
from data_import.contracts import DATASET_ID, content_hash, validate
from data_import.models import AcademicCoverage, Coverage, ImportRevision
from data_import.services import check_integrity, get_head, read_snapshot
from profiles.forms import safe_image
from profiles.models import Avatar, GroupResource, GroupResourceRevision, Preferences
from studies.models import Period, StudentProfile


MEMBERS = (
    ('SP-T11-DARIA', 'classmate_daria', 'Дарья Волкова', 'context', 'blue'),
    ('SP-T11-KIRILL', 'classmate_kirill', 'Кирилл Орлов', 'context', None),
    ('SP-T11-SOFIA', 'classmate_sofia', 'София Лебедева', 'private', 'slate'),
    ('SP-T11-MARK', 'classmate_mark', 'Марк Соколов', 'private', None),
)

RESOURCES = (
    ('materials', 'Как подготовить материал',
     'Проверьте условия своего преподавателя в «Учёбе». Сохраните черновик или отправьте текущую версию. Отзыв хранится рядом с отправленной работой и не заменяет официальную оценку.'),
    ('requests', 'Как обратиться в деканат',
     'В «Заявках» выберите справку или исправление учебных сведений. Укажите нужные данные и следите за ответом сотрудника в деталях заявки. Демонстрация использует вымышленные сведения.'),
)


def _check_members(owner):
    """Любое совпадение служебного ключа обязано быть тем же synthetic record."""
    for source_id, username, name, _, _ in MEMBERS:
        profile = StudentProfile.objects.filter(dataset=owner.dataset, source_id=source_id).first()
        if profile and (profile.name != name or profile.group_id != owner.group_id or profile.year != owner.year):
            raise ValueError(f'Конфликт учебного профиля {source_id}; существующая запись сохранена.')
        user = get_user_model().objects.filter(username=username).first()
        if user:
            identity = Identity.objects.filter(user=user).first()
            if (not profile or not identity or identity.role != 'student' or identity.profile_id != profile.pk
                    or user.is_staff or user.is_superuser or user.groups.exists() or user.user_permissions.exists()
                    or StaffScope.objects.filter(identity=identity).exists()):
                raise ValueError(f'Конфликт identity {username}; переназначение запрещено.')


def prepare_directory_batch(*, expected_revision=None):
    """Прочитать consistent head; перенести полноту без новых обещаний о данных."""
    with read_snapshot():
        head = get_head()
        check_integrity(head)
        if expected_revision is not None and head.current_revision != expected_revision:
            raise ValueError('Текущая revision отличается от ожидаемой; подготовьте новый preview.')
        revision = ImportRevision.objects.get(dataset=head, revision=head.current_revision)
        if revision.schema_version != 4:
            raise ValueError('Подготовка TASK11 требует принятого набора schema 4.')
        owner = StudentProfile.objects.select_related('group', 'dataset').get(dataset=head, source_id='SP-A')
        _check_members(owner)
        existing = set(StudentProfile.objects.filter(dataset=head).values_list('source_id', flat=True))
        source_time = timezone.now().isoformat()
        operations = [
            {'operation': 'create', 'entity_type': 'profile', 'record': {
                'source_id': source_id, 'record_revision': 1, 'source_updated_at': source_time,
                'name': name, 'year': owner.year, 'group_id': owner.group.source_id}}
            for source_id, _, name, _, _ in MEMBERS if source_id not in existing
        ]
        if not operations:
            return None, head.current_revision
        coverage = [{
            'entity_type': 'lesson', 'group_id': row.group.source_id, 'period_id': row.period.source_id,
            'date_from': row.date_from.isoformat(), 'date_until': row.date_until.isoformat(),
            'completeness': row.completeness, 'active_ids': row.active_ids, 'active_count': row.active_count,
        } for row in Coverage.objects.filter(revision=revision).select_related('group', 'period').order_by('pk')]
        academic = [{
            'entity_type': row.entity_type, 'student_id': row.student.source_id, 'period_id': row.period.source_id,
            'completeness': row.completeness, 'record_ids': row.record_ids, 'record_count': row.record_count,
        } for row in AcademicCoverage.objects.filter(revision=revision).select_related('student', 'period').order_by('pk')]
        payload = {
            'schema_version': 4, 'subset': 's4_studies_v4', 'dataset_id': DATASET_ID,
            'dataset_revision': head.current_revision + 1, 'parent_revision': head.current_revision,
            'provenance': 'synthetic', 'prepared_by': 'TASK11 directory provisioning',
            'preparation_reason': 'Добавить четыре вымышленных профиля группы без изменения существующих учебных записей.',
            'source_as_of': source_time, 'valid_from': revision.valid_from.isoformat(),
            'valid_until': revision.valid_until.isoformat(), 'operations': operations,
            'coverage': coverage, 'academic_coverage': academic,
        }
        payload['content_sha256'] = content_hash(payload)
        return validate(payload), head.current_revision


def synthetic_avatar(palette):
    """Собственный геометрический портрет; без реального лица и метаданных."""
    background, figure, accent = {
        'blue': ('#d8e8f4', '#386a91', '#223b59'),
        'slate': ('#e0e4f0', '#596985', '#334158'),
    }[palette]
    canvas = Image.new('RGB', (256, 256), background)
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((69, 37, 187, 155), fill=figure)
    draw.ellipse((33, 150, 223, 340), fill=accent)
    buffer = BytesIO()
    canvas.save(buffer, format='PNG')
    return safe_image(SimpleUploadedFile('synthetic-portrait.png', buffer.getvalue(), content_type='image/png'))


def provision_directory(*, operator):
    """Provisioning после принятого импорта; никаких изменений старых identities."""
    if not operator.strip() or len(operator) > 100:
        raise ValueError('Укажите оператора до 100 символов.')
    with transaction.atomic():
        guard = lock_guard()
        owner = StudentProfile.objects.select_related('group__programme', 'dataset').get(source_id='SP-A', dataset_id=DATASET_ID)
        _check_members(owner)
        for source_id, _, _, _, _ in MEMBERS:
            if not StudentProfile.objects.filter(dataset=owner.dataset, source_id=source_id).exists():
                raise ValueError('Сначала примите подготовленные учебные профили через версионный импорт.')
        publisher = Identity.objects.get(user__username='staff_demo', role='staff')
        if not StaffScope.objects.filter(identity=publisher, faculty_id=owner.group.programme.faculty_id).exists():
            raise ValueError('У staff_demo нет действующей области публикации для группы.')
        created = photos = resources = 0
        for source_id, username, _, audience, palette in MEMBERS:
            if get_user_model().objects.filter(username=username).exists():
                continue
            profile = StudentProfile.objects.get(dataset=owner.dataset, source_id=source_id)
            user = get_user_model().objects.create_user(username=username, password=None)
            identity = Identity.objects.create(user=user, role='student', profile=profile)
            Preferences.objects.create(user=user, photo_audience=audience)
            created += 1
            if palette:
                photo = synthetic_avatar(palette)
                Avatar.objects.create(identity=identity, image=photo, digest=sha256(photo).hexdigest())
                photos += 1
        period = Period.objects.filter(dataset=owner.dataset).order_by('-date_until', '-pk').first()
        start = datetime.combine(period.date_from, time.min, ZoneInfo('Europe/Moscow'))
        end = datetime.combine(period.date_until, time.min, ZoneInfo('Europe/Moscow'))
        for key, title, description in RESOURCES:
            resource, new = GroupResource.objects.get_or_create(demo_key='task11-directory-' + key, defaults={
                'group': owner.group, 'publisher': publisher, 'category': 'document', 'title': title,
                'description': description, 'available_from': start, 'available_until': end,
            })
            if new:
                resources += 1
                GroupResourceRevision.objects.create(resource=resource, version=1, actor=publisher, payload={
                    'title': title, 'description': description, 'category': 'document', 'url': '',
                    'group_id': owner.group_id, 'available_from': start.isoformat(), 'available_until': end.isoformat(),
                    'operator': operator, 'synthetic': True,
                })
        if created or resources:
            guard.revision += 1
            guard.save(update_fields=['revision'])
            AccessAudit.objects.create(action='directory_provision', revision=guard.revision, operator=operator)
        return {'identities_created': created, 'photos_created': photos, 'resources_created': resources}
