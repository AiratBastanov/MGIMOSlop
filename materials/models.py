"""Собственные материалы кабинета: импорт не владеет этими записями."""
from django.db import models
from django.db.models import F, Q
from django.conf import settings
import uuid
from django.utils import timezone


class TeachingAssignment(models.Model):
    teacher = models.ForeignKey('accounts.Identity', on_delete=models.PROTECT)
    course = models.ForeignKey('studies.Course', on_delete=models.PROTECT)
    group = models.ForeignKey('studies.Group', on_delete=models.PROTECT)
    period = models.ForeignKey('studies.Period', on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    available_from = models.DateTimeField()
    available_until = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['teacher','course','group','period'], name='teaching_assignment_unique'),
            models.CheckConstraint(condition=Q(available_until__gt=F('available_from')), name='teaching_assignment_interval'),
        ]


class SubmissionPolicy(models.Model):
    task_revision = models.OneToOneField('workspaces.TeachingRevision', null=True, on_delete=models.PROTECT, related_name='submission_policy')
    assignment = models.ForeignKey(TeachingAssignment, on_delete=models.PROTECT, related_name='policies')
    version = models.PositiveIntegerField()
    modes = models.JSONField()
    text_limit = models.PositiveIntegerField(default=5000)
    cloud_hosts = models.JSONField(default=list)
    instructions = models.CharField(max_length=500, blank=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    attachments_allowed = models.BooleanField(default=False)
    attachment_types = models.JSONField(default=list)
    attachment_count = models.PositiveSmallIntegerField(default=5)
    attachment_file_limit = models.PositiveIntegerField(default=10 * 1024 * 1024)
    attachment_total_limit = models.PositiveIntegerField(default=25 * 1024 * 1024)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['assignment','version'], name='submission_policy_version'),
            models.CheckConstraint(condition=Q(version__gte=1), name='submission_policy_positive_version'),
            models.CheckConstraint(condition=Q(text_limit__gte=100,text_limit__lte=20000), name='submission_policy_text_limit'),
        ]


class Material(models.Model):
    task = models.ForeignKey('workspaces.CourseTask', null=True, on_delete=models.PROTECT, related_name='responses')
    owner = models.ForeignKey('studies.StudentProfile', on_delete=models.PROTECT)
    assignment = models.ForeignKey(TeachingAssignment, on_delete=models.PROTECT)
    draft_title = models.CharField(max_length=160)
    draft_mode = models.CharField(max_length=10, default='text')
    draft_content = models.TextField(blank=True)
    draft_version = models.PositiveIntegerField(default=1)
    has_draft = models.BooleanField(default=True)
    submission_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['owner','task'], condition=Q(task__isnull=False), name='one_response_per_task_student'),
            models.CheckConstraint(condition=Q(draft_version__gte=1), name='material_draft_version'),
            models.CheckConstraint(condition=Q(draft_mode__in=['text','link','email']), name='material_draft_mode'),
        ]


class MaterialRevision(models.Model):
    task_revision = models.ForeignKey('workspaces.TeachingRevision', null=True, on_delete=models.PROTECT, related_name='submitted_responses')
    task_conditions = models.JSONField(default=dict)
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='revisions')
    number = models.PositiveIntegerField()
    title = models.CharField(max_length=160)
    mode = models.CharField(max_length=10)
    content = models.TextField()
    policy = models.ForeignKey(SubmissionPolicy, on_delete=models.PROTECT)
    submitted_at = models.DateTimeField(default=timezone.now)
    attachment_manifest = models.JSONField(default=list)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['material','number'], name='material_revision_unique'),
            models.CheckConstraint(condition=Q(number__gte=1), name='material_revision_positive'),
            models.CheckConstraint(condition=Q(mode__in=['text','link','email']), name='material_revision_mode'),
        ]


class MaterialReview(models.Model):
    result = models.CharField(max_length=10, blank=True)
    revision = models.OneToOneField(MaterialRevision, on_delete=models.PROTECT, related_name='review')
    teacher = models.ForeignKey('accounts.Identity', on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=[('accepted','Принято'),('changes_requested','Нужны исправления')])
    feedback = models.TextField(max_length=2000)
    reviewed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(status__in=['accepted','changes_requested']), name='material_review_status')]


class MaterialAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='stored_attachments')
    name = models.CharField(max_length=160)
    kind = models.CharField(max_length=8)
    mime = models.CharField(max_length=100)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    data = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)


class DraftAttachment(models.Model):
    material = models.ForeignKey(Material, on_delete=models.PROTECT, related_name='draft_files')
    attachment = models.ForeignKey(MaterialAttachment, on_delete=models.PROTECT)
    position = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['material','attachment'], name='material_draft_file_unique'),
            models.UniqueConstraint(fields=['material','position'], name='material_draft_position_unique'),
        ]


class RevisionAttachment(models.Model):
    revision = models.ForeignKey(MaterialRevision, on_delete=models.PROTECT, related_name='files')
    attachment = models.ForeignKey(MaterialAttachment, on_delete=models.PROTECT)
    position = models.PositiveSmallIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['revision','attachment'], name='material_revision_file_unique'),
            models.UniqueConstraint(fields=['revision','position'], name='material_revision_position_unique'),
        ]


class MaterialOperation(models.Model):
    owner = models.ForeignKey('studies.StudentProfile', on_delete=models.PROTECT)
    key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    material = models.ForeignKey(Material, on_delete=models.PROTECT)
    action = models.CharField(max_length=10)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['owner','key'], name='material_operation_unique')]
