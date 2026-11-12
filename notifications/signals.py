"""Receipt/outbox creation participates in the event's database transaction."""
from django.db.models.signals import post_save
from django.dispatch import receiver
from .models import Notification
from accounts.models import Identity
from studies.models import StudentProfile


@receiver(post_save, sender=Notification)
def new_notification(sender, instance, created, raw=False, **kwargs):
    if created and not raw:
        from .delivery import distribute
        distribute(instance)


@receiver(post_save, sender=Identity)
def provisioned_recipient(sender, instance, created, raw=False, **kwargs):
    if created and not raw and instance.role == 'student':
        from .delivery import sync_recipient
        sync_recipient(instance.user_id)
