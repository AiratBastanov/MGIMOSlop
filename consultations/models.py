"""Встречи принадлежат приложению; они никогда не становятся Lesson."""
from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import F, Q


def interval():
    return models.Func('starts_at', 'ends_at', models.Value('[)'),
                       function='TSTZRANGE', output_field=DateTimeRangeField())


class ConsultationSlot(models.Model):
    host = models.ForeignKey('accounts.Identity', on_delete=models.PROTECT)
    kind = models.CharField(max_length=8)
    assignment = models.ForeignKey('materials.TeachingAssignment', null=True, on_delete=models.PROTECT)
    faculty = models.ForeignKey('studies.Faculty', null=True, on_delete=models.PROTECT)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    topic = models.CharField(max_length=160)
    venue = models.CharField(max_length=240, blank=True)
    meeting_url = models.CharField(max_length=1000, blank=True)
    state = models.CharField(max_length=10, default='open')
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    demo_key = models.CharField(max_length=100, unique=True, null=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=F('starts_at')), name='consult_slot_interval'),
            models.CheckConstraint(condition=Q(state__in=['open', 'closed', 'cancelled']), name='consult_slot_state'),
            models.CheckConstraint(condition=(Q(kind='teacher', assignment__isnull=False, faculty__isnull=True) |
                Q(kind='staff', assignment__isnull=True, faculty__isnull=False)), name='consult_slot_audience'),
            ExclusionConstraint(name='consult_host_overlap', expressions=[('host', RangeOperators.EQUAL),
                (interval(), RangeOperators.OVERLAPS)], condition=Q(state='open')),
        ]
        indexes = [models.Index(fields=['starts_at', 'state'])]


class Booking(models.Model):
    slot = models.ForeignKey(ConsultationSlot, on_delete=models.PROTECT, related_name='bookings')
    student = models.ForeignKey('studies.StudentProfile', on_delete=models.PROTECT)
    # Exact immutable slot interval permits a database-level cross-slot exclusion.
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    state = models.CharField(max_length=10, default='booked')
    version = models.PositiveIntegerField(default=1)
    question = models.CharField(max_length=1000, blank=True)
    request = models.ForeignKey('student_requests.Request', null=True, on_delete=models.PROTECT)
    academic_ack = models.BooleanField(default=False)
    replaces = models.OneToOneField('self', null=True, on_delete=models.PROTECT, related_name='replacement')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(ends_at__gt=F('starts_at')), name='consult_booking_interval'),
            models.CheckConstraint(condition=Q(state__in=['booked', 'cancelled', 'completed']), name='consult_booking_state'),
            models.UniqueConstraint(fields=['slot'], condition=Q(state='booked'), name='consult_slot_capacity_one'),
            ExclusionConstraint(name='consult_student_overlap', expressions=[('student', RangeOperators.EQUAL),
                (interval(), RangeOperators.OVERLAPS)], condition=Q(state='booked')),
        ]
        indexes = [models.Index(fields=['student', 'starts_at'])]


class ConsultationEvent(models.Model):
    """Неизменная история; вопрос и связанная заявка не входят в уведомление."""
    slot = models.ForeignKey(ConsultationSlot, on_delete=models.PROTECT, related_name='events')
    booking = models.ForeignKey(Booking, null=True, on_delete=models.PROTECT, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    action = models.CharField(max_length=20)
    slot_version = models.PositiveIntegerField()
    booking_version = models.PositiveIntegerField(null=True)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(action__in=[
            'created', 'closed', 'slot_cancelled', 'booked', 'cancelled', 'transferred', 'venue', 'completed']),
            name='consult_event_action')]


class ConsultationOperation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    result = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'key'], name='consult_operation_identity')]
