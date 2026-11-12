import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q, F


class AuthorizationGuard(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(id=1), name='single_authorization_guard')]


class Identity(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    role = models.CharField(max_length=10, choices=[('student','Студент'),('staff','Сотрудник'),('teacher','Преподаватель')])
    profile = models.OneToOneField('studies.StudentProfile', null=True, on_delete=models.PROTECT)
    instructor = models.OneToOneField('studies.Instructor', null=True, blank=True, on_delete=models.PROTECT)
    display_name = models.CharField(max_length=120, blank=True)
    access_revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        constraints = [models.CheckConstraint(condition=(
            Q(role='student',profile__isnull=False,instructor__isnull=True) |
            Q(role='staff',profile__isnull=True,instructor__isnull=True) |
            Q(role='teacher',profile__isnull=True,instructor__isnull=False)), name='identity_role_binding')]


class StaffScope(models.Model):
    identity = models.OneToOneField(Identity, on_delete=models.PROTECT)
    faculty = models.ForeignKey('studies.Faculty', on_delete=models.PROTECT)


class SessionContext(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    session_digest = models.CharField(max_length=64, unique=True)
    authenticated_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(expires_at__gt=F('authenticated_at')), name='session_context_interval')]


class AccessAudit(models.Model):
    recorded_at = models.DateTimeField(auto_now_add=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT)
    action = models.CharField(max_length=40)
    revision = models.PositiveBigIntegerField()
    operator = models.CharField(max_length=100)
