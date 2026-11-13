from django.conf import settings
from django.db import models
from django.db.models import Q


class Journey(models.Model):
    owner = models.ForeignKey('studies.StudentProfile', on_delete=models.PROTECT)
    template_key = models.CharField(max_length=20)
    template_revision = models.PositiveSmallIntegerField(default=1)
    version = models.PositiveIntegerField(default=1)
    state = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['owner','template_key','template_revision'], name='journey_owner_template'),
            models.CheckConstraint(condition=Q(template_key__in=['exchange','correction'],template_revision=1,version__gte=1), name='journey_known_template'),
        ]


class JourneyRevision(models.Model):
    journey = models.ForeignKey(Journey, on_delete=models.PROTECT, related_name='revisions')
    version = models.PositiveIntegerField()
    state = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['journey','version'], name='journey_revision_unique')]


class JourneySubmission(models.Model):
    journey = models.ForeignKey(Journey, on_delete=models.PROTECT, related_name='submissions')
    revision = models.ForeignKey(JourneyRevision, on_delete=models.PROTECT)
    request = models.OneToOneField('student_requests.Request', on_delete=models.PROTECT, related_name='journey_submission')
    created_at = models.DateTimeField(auto_now_add=True)


class JourneyOperation(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    journey = models.ForeignKey(Journey, on_delete=models.PROTECT)
    outcome = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user','key'], name='journey_operation_unique')]


class JourneyBooking(models.Model):
    journey = models.ForeignKey(Journey, on_delete=models.PROTECT, related_name='booking_links')
    booking = models.ForeignKey('consultations.Booking', on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['journey','booking'], name='journey_booking_unique')]
