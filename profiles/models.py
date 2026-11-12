from django.conf import settings
from django.db import models
from django.db.models import F, Q


class Preferences(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    contact_email = models.EmailField(blank=True)
    email_verified_at = models.DateTimeField(null=True)
    email_revision = models.PositiveIntegerField(default=1)
    email_enabled = models.BooleanField(default=False)
    email_requests = models.BooleanField(default=True)
    email_academic = models.BooleanField(default=True)
    email_announcements = models.BooleanField(default=True)
    photo_audience = models.CharField(max_length=12, default='private', choices=[('private', 'Только я'), ('context', 'Моя группа и назначенные преподаватели')])
    version = models.PositiveIntegerField(default=1)
    share_email = models.BooleanField(default=False)
    directory_phone = models.CharField(max_length=24, blank=True)
    share_phone = models.BooleanField(default=False)


class Avatar(models.Model):
    """Sanitized JPEG in PostgreSQL: backup and restore include the photo bytes."""
    identity = models.OneToOneField('accounts.Identity', on_delete=models.PROTECT)
    image = models.BinaryField()
    digest = models.CharField(max_length=64)
    updated_at = models.DateTimeField(auto_now=True)


class GroupResource(models.Model):
    archived = models.BooleanField(default=False)
    group = models.ForeignKey('studies.Group', on_delete=models.PROTECT)
    publisher = models.ForeignKey('accounts.Identity', on_delete=models.PROTECT)
    category = models.CharField(max_length=16, choices=[('internship', 'Практика'), ('course', 'Курсы'), ('document', 'Документы')])
    title = models.CharField(max_length=160)
    description = models.TextField(max_length=2000)
    url = models.URLField(max_length=500, blank=True)
    available_from = models.DateTimeField()
    available_until = models.DateTimeField()
    version = models.PositiveIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)
    demo_key = models.CharField(max_length=80, null=True, unique=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(available_until__gt=F('available_from')), name='group_resource_interval')]


class GroupResourceRevision(models.Model):
    resource = models.ForeignKey(GroupResource, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    actor = models.ForeignKey('accounts.Identity', on_delete=models.PROTECT)
    payload = models.JSONField()
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['resource', 'version'], name='resource_revision_unique')]
