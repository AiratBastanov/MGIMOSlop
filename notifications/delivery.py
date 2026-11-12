"""Durable per-recipient receipt and email outbox. Never sends during a request."""
from datetime import timedelta
from uuid import UUID
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from accounts.access import Actor, identity_for, AccessDenied, lock_guard
from accounts.models import Identity
from profiles.models import Preferences
from .models import Notification, NotificationReceipt, EmailOutbox


def delivery_actor(user_id, now):
    _, identity = identity_for(user_id)
    return Actor(user_id, identity.role, identity.profile_id,
        identity.profile.group_id if identity.profile_id else None, '', UUID(int=0),
        now + timedelta(seconds=30), identity, now)


def category(notification):
    if notification.application_event_id:
        return 'requests' if notification.application_event.kind=='clarification' else 'announcements' if notification.application_event.kind in ('notice','resource') else 'academic'
    if notification.request_event_id:
        return 'requests'
    if notification.announcement_change_id:
        return 'announcements'
    return 'academic'


def eligible(actor, notification, now):
    from accounts.selectors import domain_notifications
    if notification.application_event_id:
        from workspaces.events import event_scope
        return notification.published_at<=now and event_scope(actor,now,archive=True).filter(pk=notification.application_event_id).exists()
    if actor.role=='teacher':return False
    if actor.role=='staff':
        from student_requests.selectors import request_scope
        return bool(notification.request_event_id and notification.published_at<=now and request_scope(actor).filter(pk=notification.request_event.request_id).exists())
    return domain_notifications(actor, now=now, archive=True,include_application=False).filter(pk=notification.pk).exists()


def register(notification, user_id, now, *, queue_email=True):
    available = max(now, notification.published_at)
    actor = delivery_actor(user_id, available)
    if not eligible(actor, notification, available):
        return
    receipt, created = NotificationReceipt.objects.get_or_create(user_id=user_id,
        notification=notification, defaults={'available_at': available})
    if not created or not queue_email:
        return
    pref = Preferences.objects.filter(user_id=user_id, email_enabled=True,
        email_verified_at__isnull=False).first()
    if pref and pref.contact_email and getattr(pref, 'email_' + category(notification)):
        EmailOutbox.objects.get_or_create(user_id=user_id, notification=notification,
            defaults={'email_revision': pref.email_revision, 'address': pref.contact_email,
                      'next_attempt_at': available})


def distribute(notification):
    now = timezone.now()
    for uid in Identity.objects.filter(user__is_active=True).values_list('user_id', flat=True):
        try:
            register(notification, uid, now)
        except AccessDenied:
            continue


def sync_recipient(user_id, *, queue_email=False):
    """Explicit provisioning/scope-change hook; never called from GET."""
    now = timezone.now()
    try:
        actor = delivery_actor(user_id, now)
    except AccessDenied:
        return
    # Include already-committed future publications. register() evaluates their
    # recipient scope at the publication instant and starts the clock then.
    known = NotificationReceipt.objects.filter(user_id=user_id).values('notification_id')
    for item in Notification.objects.exclude(pk__in=known).iterator():
        register(item, user_id, now, queue_email=queue_email)


def queue_verification(pref):
    # Address changes cancel old work by revision; only this current address is used.
    return EmailOutbox.objects.create(user_id=pref.user_id, purpose='verification',
        email_revision=pref.email_revision, address=pref.contact_email, next_attempt_at=timezone.now())


def current_delivery(row, now):
    try:
        actor = delivery_actor(row.user_id, now)
    except AccessDenied:
        return False
    pref = Preferences.objects.filter(user_id=row.user_id).first()
    if not pref or pref.email_revision != row.email_revision or pref.contact_email != row.address:
        return False
    if row.purpose == 'verification':
        return pref.email_verified_at is None and row.created_at > now - timedelta(hours=1)
    if not pref.email_enabled or not pref.email_verified_at or not getattr(pref, 'email_' + category(row.notification)):
        return False
    from .selectors import inbox_events
    return inbox_events(actor, now=now).filter(pk=row.notification_id).exists()


def deliver_pending(*, limit=100, verification_key=None):
    """At-least-once attempts. A crash after transport acceptance can duplicate mail."""
    from django.conf import settings
    from django.core import signing
    from django.core.mail import EmailMessage
    from django.core.exceptions import ImproperlyConfigured
    stats = {'queued': 0, 'attempted': 0, 'captured': 0, 'accepted': 0, 'failed': 0, 'cancelled': 0, 'confirmed': 0}
    now = timezone.now()
    stats['queued'] = EmailOutbox.objects.filter(status__in=['queued', 'failed', 'attempting']).count()
    for _ in range(limit):
        with transaction.atomic():
            lock_guard()
            now = timezone.now()
            pending = EmailOutbox.objects.select_for_update(skip_locked=True).filter(
                Q(status__in=['queued', 'failed'], next_attempt_at__lte=now) |
                Q(status='attempting', attempted_at__lt=now-timedelta(minutes=5))
            )
            # Explicit local provisioning may capture only its own verification.
            if verification_key is not None:
                pending = pending.filter(key=verification_key, purpose='verification')
            row = pending.order_by('next_attempt_at', 'pk').first()
            if row is None:
                break
            if not current_delivery(row, now):
                row.status = 'cancelled'
                row.save(update_fields=['status'])
                stats['cancelled'] += 1
                continue
            row.status, row.attempted_at = 'attempting', now
            row.attempts += 1
            row.save(update_fields=['status', 'attempted_at', 'attempts'])
        # Network/backend work is outside the database request/claim transaction.
        stats['attempted'] += 1
        try:
            local = settings.EMAIL_BACKEND in ('notifications.mail_backend.CaptureBackend', 'django.core.mail.backends.locmem.EmailBackend')
            if not local and not settings.MAIL_EXTERNAL_ENABLED:
                raise ImproperlyConfigured('external_delivery_disabled')
            if row.purpose == 'verification':
                token = signing.dumps({'key': str(row.key), 'revision': row.email_revision}, salt='notification-address')
                link = settings.PUBLIC_BASE_URL.rstrip('/') + '/profile/verify-email/?token=' + token
                subject = 'Подтверждение адреса уведомлений'
                body = 'Подтвердите адрес в своём кабинете в течение часа.\n' + link
            else:
                subject = 'Новое уведомление в кабинете'
                body = 'В кабинете появилось обновление. Для просмотра войдите в свой аккаунт.\n' + settings.PUBLIC_BASE_URL.rstrip('/') + '/notifications/'
            if not local and not settings.PUBLIC_BASE_URL.startswith('https://'):
                raise ImproperlyConfigured('https_base_required')
            count = EmailMessage(subject, body, to=[row.address], headers={'Message-ID': f'<{row.key}@cabinet.invalid>'}).send(fail_silently=False)
            if count != 1:
                raise RuntimeError('backend_did_not_accept')
        except Exception as exc:
            # Store type only: SMTP errors can contain addresses or credentials.
            EmailOutbox.objects.filter(pk=row.pk, status='attempting').update(status='failed',
                error_code=type(exc).__name__, next_attempt_at=timezone.now()+timedelta(minutes=min(60, 2**min(row.attempts, 6))))
            stats['failed'] += 1
        else:
            state = 'captured' if local else 'accepted'
            EmailOutbox.objects.filter(pk=row.pk, status='attempting').update(status=state, accepted_at=timezone.now(), error_code='')
            stats[state] += 1
    return stats
