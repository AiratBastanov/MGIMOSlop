from django.db import models
from django.conf import settings
from django.db.models import F, Q
from studies.models import SourceRecord


class Announcement(SourceRecord):
    """Один импортируемый факт; due_at — срок объявления, не обязанность."""
    title = models.CharField(max_length=180)
    body = models.TextField(max_length=4000)
    audience = models.CharField(max_length=20)
    faculty = models.ForeignKey('studies.Faculty', null=True, on_delete=models.PROTECT)
    group = models.ForeignKey('studies.Group', null=True, on_delete=models.PROTECT)
    student = models.ForeignKey('studies.StudentProfile', null=True, on_delete=models.PROTECT)
    published_at = models.DateTimeField()
    visible_until = models.DateTimeField(null=True)
    due_at = models.DateTimeField(null=True)
    status = models.CharField(max_length=12)

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(status__in=['draft','published','withdrawn']), name='announcement_status'),
            models.CheckConstraint(condition=~Q(title='') & ~Q(body=''), name='announcement_text'),
            models.CheckConstraint(condition=Q(visible_until__isnull=True) | Q(visible_until__gt=F('published_at')), name='announcement_visibility_interval'),
            models.CheckConstraint(condition=Q(due_at__isnull=True) | Q(due_at__gte=F('published_at')), name='announcement_deadline_interval'),
            models.CheckConstraint(condition=(
                Q(audience='all_students', faculty__isnull=True, group__isnull=True, student__isnull=True) |
                Q(audience='faculty', faculty__isnull=False, group__isnull=True, student__isnull=True) |
                Q(audience='group', faculty__isnull=True, group__isnull=False, student__isnull=True) |
                Q(audience='student', faculty__isnull=True, group__isnull=True, student__isnull=False)
            ), name='announcement_audience_target'),
        ]


class AnnouncementChange(models.Model):
    dataset = models.ForeignKey('data_import.Dataset', on_delete=models.PROTECT)
    revision = models.ForeignKey('data_import.ImportRevision', on_delete=models.PROTECT)
    announcement = models.ForeignKey(Announcement, on_delete=models.PROTECT)
    event_id = models.CharField(max_length=80)
    change_type = models.CharField(max_length=20)
    before = models.JSONField(null=True)
    after = models.JSONField()
    reason = models.CharField(max_length=500)
    operator = models.CharField(max_length=100)
    source_updated_at = models.DateTimeField()
    recorded_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['dataset','event_id'], name='announcement_event_identity'),
            models.CheckConstraint(condition=Q(change_type__in=['create','correction','publish','withdraw']), name='announcement_change_type'),
        ]

def notification_target_constraint():
    targets=['schedule_change','announcement_change','final_result_change','requirement_change','assessment_change','request_event','application_event']
    condition=Q(pk__in=[])
    for target in targets:
        condition |= Q(**{name+'__isnull':name!=target for name in targets},group__isnull=target!='schedule_change')
    return models.CheckConstraint(condition=condition,name='notification_typed_target')


class Notification(models.Model):
    application_event = models.OneToOneField("workspaces.ApplicationEvent", null=True, on_delete=models.PROTECT)
    # XOR сохраняет прежние PK и Read: новое событие не заменяет старое.
    schedule_change = models.OneToOneField('studies.ScheduleChange', null=True, on_delete=models.PROTECT)
    group = models.ForeignKey('studies.Group', null=True, on_delete=models.PROTECT)
    announcement_change = models.OneToOneField(AnnouncementChange, null=True, on_delete=models.PROTECT)
    final_result_change = models.OneToOneField('studies.FinalResultChange', null=True, on_delete=models.PROTECT)
    requirement_change = models.OneToOneField('studies.RequirementChange', null=True, on_delete=models.PROTECT)
    assessment_change = models.OneToOneField('studies.AssessmentChange', null=True, on_delete=models.PROTECT)
    request_event = models.OneToOneField('student_requests.RequestEvent', null=True, on_delete=models.PROTECT)
    published_at = models.DateTimeField()

    class Meta:
        constraints = [notification_target_constraint()]


class NotificationRead(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    notification = models.ForeignKey(Notification, on_delete=models.PROTECT)
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','notification'], name='notification_read_owner')]


class InboxClear(models.Model):
    import uuid
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    cutoff = models.DateTimeField()
    undone_at = models.DateTimeField(null=True)


class NotificationReceipt(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    notification = models.ForeignKey(Notification, on_delete=models.PROTECT)
    available_at = models.DateTimeField()
    dismissed_at = models.DateTimeField(null=True)
    restored_at = models.DateTimeField(null=True)
    clear_batch = models.ForeignKey(InboxClear, null=True, on_delete=models.PROTECT)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'notification'], name='notification_receipt_owner')]
        indexes = [models.Index(fields=['user', 'available_at'])]


class EmailOutbox(models.Model):
    import uuid
    key = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    notification = models.ForeignKey(Notification, null=True, on_delete=models.PROTECT)
    purpose = models.CharField(max_length=12, default='notification')
    email_revision = models.PositiveIntegerField()
    address = models.EmailField()
    created_at = models.DateTimeField(auto_now_add=True)
    next_attempt_at = models.DateTimeField()
    attempted_at = models.DateTimeField(null=True)
    accepted_at = models.DateTimeField(null=True)
    confirmed_at = models.DateTimeField(null=True)
    attempts = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=12, default='queued')
    error_code = models.CharField(max_length=80, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'notification'], condition=Q(notification__isnull=False), name='outbox_event_recipient')]
