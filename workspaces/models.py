"""Собственные публикации и обмен уточнениями. Не academic source records."""
import uuid
from django.conf import settings
from django.db import models
from django.db.models import Q


class TeachingPublication(models.Model):
    assignment = models.ForeignKey('materials.TeachingAssignment', on_delete=models.PROTECT)
    kind = models.CharField(max_length=8, choices=[('resource', 'Материал'), ('task', 'Задание')])
    draft = models.JSONField(default=dict)
    draft_files = models.JSONField(default=list)
    version = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=12, default='draft')
    published_revision = models.ForeignKey('TeachingRevision', null=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    demo_key = models.CharField(max_length=100, null=True, unique=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(kind__in=['resource','task']), name='teaching_content_kind'),
            models.CheckConstraint(condition=Q(state__in=['draft','published','withdrawn','archived']), name='teaching_content_state')]


class TeachingRevision(models.Model):
    publication = models.ForeignKey(TeachingPublication, on_delete=models.PROTECT, related_name='revisions')
    number = models.PositiveIntegerField()
    payload = models.JSONField()
    files = models.JSONField(default=list)
    state = models.CharField(max_length=12)
    author = models.ForeignKey('accounts.Identity', on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['publication','number'], name='teaching_revision_number')]


class PublicationFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    publication = models.ForeignKey(TeachingPublication, on_delete=models.PROTECT, related_name='stored_files')
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=8)
    mime = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    data = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)


class CourseTask(models.Model):
    """Выданное задание; TeachingAssignment задаёт полномочия, Material — ответ."""
    publication = models.OneToOneField(TeachingPublication, on_delete=models.PROTECT, related_name='task')


class TaskMember(models.Model):
    task = models.ForeignKey(CourseTask, on_delete=models.PROTECT, related_name='members')
    student = models.ForeignKey('studies.StudentProfile', on_delete=models.PROTECT)
    assigned_at = models.DateTimeField()
    assigned_revision = models.ForeignKey(TeachingRevision, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['task','student'], name='task_cohort_member')]


class StaffNotice(models.Model):
    faculty = models.ForeignKey('studies.Faculty', on_delete=models.PROTECT)
    group = models.ForeignKey('studies.Group', null=True, on_delete=models.PROTECT)
    author = models.ForeignKey('accounts.Identity', on_delete=models.PROTECT)
    draft = models.JSONField(default=dict)
    version = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=12, default='draft')
    published_revision = models.ForeignKey('StaffNoticeRevision', null=True, on_delete=models.PROTECT, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    demo_key = models.CharField(max_length=100, null=True, unique=True)


class StaffNoticeRevision(models.Model):
    notice = models.ForeignKey(StaffNotice, on_delete=models.PROTECT, related_name='revisions')
    number = models.PositiveIntegerField()
    payload = models.JSONField()
    state = models.CharField(max_length=12)
    author = models.ForeignKey('accounts.Identity', on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['notice','number'], name='staff_notice_revision_number')]


class Clarification(models.Model):
    request = models.ForeignKey('student_requests.Request', on_delete=models.PROTECT, related_name='clarifications')
    state = models.CharField(max_length=10, default='open')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['request'], condition=Q(state__in=['open','answered']), name='one_pending_clarification'),
            models.CheckConstraint(condition=Q(state__in=['open','answered','resolved']), name='clarification_state')]


class ClarificationMessage(models.Model):
    clarification = models.ForeignKey(Clarification, on_delete=models.PROTECT, related_name='messages')
    number = models.PositiveIntegerField()
    action = models.CharField(max_length=10)
    body = models.TextField(max_length=2000)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['clarification','number'], name='clarification_message_number')]


class WorkspaceOperation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','key'], name='workspace_operation_key')]


class ApplicationEvent(models.Model):
    """Типизированный источник существующей Notification, без второго inbox."""
    kind = models.CharField(max_length=16)
    teaching_revision = models.OneToOneField(TeachingRevision, null=True, on_delete=models.PROTECT)
    submission = models.OneToOneField('materials.MaterialRevision', null=True, on_delete=models.PROTECT)
    review = models.OneToOneField('materials.MaterialReview', null=True, on_delete=models.PROTECT)
    clarification_message = models.OneToOneField(ClarificationMessage, null=True, on_delete=models.PROTECT)
    notice_revision = models.OneToOneField(StaffNoticeRevision, null=True, on_delete=models.PROTECT)
    resource_revision = models.OneToOneField('profiles.GroupResourceRevision', null=True, on_delete=models.PROTECT)
    consultation_event = models.OneToOneField('consultations.ConsultationEvent', null=True, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
