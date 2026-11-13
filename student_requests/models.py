from django.conf import settings
from django.db import models
from django.db.models import F, Q
from .workflow import REQUEST_TYPE, REQUEST_TYPES, STATUS_LABELS, TEXT_LIMIT


class Request(models.Model):
    """Application-owned запись: учебный импорт никогда ею не владеет."""
    owner = models.ForeignKey('studies.StudentProfile', on_delete=models.PROTECT)
    servicing_faculty = models.ForeignKey('studies.Faculty', on_delete=models.PROTECT)
    request_type = models.CharField(max_length=30, default=REQUEST_TYPE)
    comment = models.TextField(max_length=TEXT_LIMIT, blank=True)
    payload_schema = models.PositiveSmallIntegerField(default=1)
    payload_data = models.JSONField(default=dict)
    applicant_snapshot = models.JSONField(default=dict)
    submission_key = models.UUIDField()
    payload_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=16, choices=list(STATUS_LABELS.items()))
    version = models.PositiveIntegerField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    provenance = models.CharField(max_length=12, default='student')

    @property
    def reference(self):
        return f'З-{self.pk:06d}'

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['owner', 'submission_key'], name='request_owner_submission'),
            models.CheckConstraint(condition=Q(request_type__in=list(REQUEST_TYPES)), name='request_supported_type'),
            models.CheckConstraint(condition=(Q(payload_schema=1, request_type=REQUEST_TYPE, payload_data={}, applicant_snapshot={}) |
                                              Q(payload_schema=2, request_type__in=['study_certificate','academic_correction']) |
                                              Q(payload_schema=3, request_type='exchange_plan_review')), name='request_payload_schema'),
            models.CheckConstraint(condition=Q(status__in=list(STATUS_LABELS)), name='request_status'),
            models.CheckConstraint(condition=Q(version__gte=1), name='request_positive_version'),
            models.CheckConstraint(condition=Q(updated_at__gte=F('created_at')), name='request_time_order'),
            models.CheckConstraint(condition=Q(provenance__in=['student', 'bootstrap']), name='request_provenance'),
            models.CheckConstraint(condition=Q(payload_fingerprint__regex=r'^[0-9a-f]{64}$'), name='request_fingerprint_shape'),
        ]
        indexes = [models.Index(fields=['owner', '-created_at'], name='request_owner_created'),
                   models.Index(fields=['servicing_faculty', 'status', '-created_at'], name='request_faculty_status')]


class RequestEvent(models.Model):
    request = models.ForeignKey(Request, on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    recorded_at = models.DateTimeField()
    action = models.CharField(max_length=12)
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16)
    version = models.PositiveIntegerField()
    explanation = models.TextField(max_length=TEXT_LIMIT, blank=True)
    operator = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['request', 'version'], name='request_event_version'),
            models.CheckConstraint(condition=Q(version__gte=1), name='request_event_positive_version'),
            models.CheckConstraint(condition=(
                Q(action='submit', from_status='', to_status='submitted', version=1, explanation='') |
                Q(action='take', from_status='submitted', to_status='in_progress', version__gt=1, explanation='') |
                (Q(action='reject', from_status__in=['submitted', 'in_progress'], to_status='rejected', version__gt=1) & ~Q(explanation='')) |
                (Q(action='complete', from_status='in_progress', to_status='ready', version__gt=1) & ~Q(explanation=''))
            ), name='request_event_transition'),
        ]
