from pathlib import Path
import json
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db import transaction
from django.utils.crypto import get_random_string
from studies.models import StudentProfile, Faculty, Instructor
from .models import Identity, StaffScope, AuthorizationGuard
from .access import lock_guard

ACCOUNTS = [('student_a','SP-A','Алина Миронова'),('student_b','SP-B','Тимур Светлов'),('staff_demo',None,'Елена Рябинина (демо)')]


def provision(credential_file):
    path = Path(credential_file)
    created = []
    credentials = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    with transaction.atomic():
        AuthorizationGuard.objects.get_or_create(pk=1)
        lock_guard()
        faculty = Faculty.objects.get(source_id='F-S2')
        for username,source_id,name in ACCOUNTS:
            profile = StudentProfile.objects.get(source_id=source_id) if source_id else None
            role = 'student' if profile else 'staff'
            user = get_user_model().objects.filter(username=username).first()
            if user:
                identity = Identity.objects.filter(user=user).first()
                if (not identity or identity.role != role or identity.profile_id != (profile.pk if profile else None)
                        or user.is_staff or user.is_superuser or user.groups.exists() or user.user_permissions.exists()):
                    raise ValueError(f'Конфликт identity: {username}; автоматическое переназначение запрещено')
                if role == 'staff' and not StaffScope.objects.filter(identity=identity,faculty=faculty).exists():
                    raise ValueError('Конфликт StaffScope')
                if role == 'student' and StaffScope.objects.filter(identity=identity).exists():
                    raise ValueError('Конфликт роли и StaffScope')
                continue
            password = get_random_string(24)
            validate_password(password)
            user = get_user_model().objects.create_user(username=username,password=password)
            identity = Identity.objects.create(user=user,profile=profile,role=role,display_name=name if role == 'staff' else '')
            if role == 'staff': StaffScope.objects.create(identity=identity,faculty=faculty)
            credentials[username] = password
            created.append(username)
        if created:
            path.parent.mkdir(parents=True,exist_ok=True)
            temp = path.with_suffix('.new')
            with temp.open('x',encoding='utf-8') as stream:
                json.dump(credentials,stream,ensure_ascii=False,indent=2)
            temp.replace(path)
    return created


def provision_teacher(credential_file, instructor_id):
    """Дополняет защищённый файл; существующие пароли/identity не меняет."""
    path=Path(credential_file)
    credentials=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    username='teacher_demo'
    with transaction.atomic():
        lock_guard()
        instructor=Instructor.objects.get(pk=instructor_id)
        user=get_user_model().objects.filter(username=username).first()
        if user:
            identity=Identity.objects.filter(user=user,role='teacher',instructor=instructor,profile__isnull=True).first()
            if (identity is None or user.is_staff or user.is_superuser or user.groups.exists()
                    or user.user_permissions.exists() or StaffScope.objects.filter(identity=identity).exists()):
                raise ValueError('Конфликт teacher_demo; автоматическое переназначение запрещено')
            return identity,False
        password=get_random_string(24)
        validate_password(password)
        user=get_user_model().objects.create_user(username=username,password=password)
        identity=Identity.objects.create(user=user,role='teacher',instructor=instructor)
        credentials[username]=password
        path.parent.mkdir(parents=True,exist_ok=True)
        temp=path.with_suffix('.teacher-new')
        with temp.open('x',encoding='utf-8') as stream:
            json.dump(credentials,stream,ensure_ascii=False,indent=2)
        temp.replace(path)
    return identity,True
