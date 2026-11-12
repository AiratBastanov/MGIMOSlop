import hashlib
from django.db import transaction
from django.core import signing
from django.utils import timezone
from accounts.access import current_actor, lock_guard, AccessDenied
from .models import Preferences, Avatar, GroupResource, GroupResourceRevision
from .selectors import publisher_groups


def update_profile(request, marker, data):
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        pref = Preferences.objects.select_for_update().filter(user_id=actor.user_id).first()
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        version = pref.version if pref else 0
        if data['version'] != version:
            raise AccessDenied('stale_context', 409)
        pref = pref or Preferences(user_id=actor.user_id, version=0)
        changes_audience = pref.photo_audience != data['photo_audience'] or data.get('remove_photo')
        changed_email = pref.contact_email != data['contact_email']
        directory_changed = any(getattr(pref, field) != data.get(field, False if field.startswith('share_') else '') for field in ('share_email','directory_phone','share_phone')) or (pref.share_email and changed_email)
        if data.get('share_email') and (changed_email or not pref.email_verified_at):
            raise AccessDenied('invalid_directory_email',400)
        if actor.role != 'student' and (data.get('share_email') or data.get('share_phone') or data.get('directory_phone')):
            raise AccessDenied('forbidden',403)
        if changed_email:
            pref.email_revision += 1
            pref.email_verified_at = None
        for field in ('contact_email', 'email_enabled', 'email_requests', 'email_academic', 'email_announcements', 'photo_audience'):
            setattr(pref, field, data[field])
        for field in ('share_email','directory_phone','share_phone'):
            setattr(pref,field,data.get(field,False if field.startswith('share_') else ''))
        if changed_email: pref.share_email=False
        pref.version += 1
        pref.save()
        if data.get('remove_photo'):
            Avatar.objects.filter(identity=actor.identity).delete()
        elif data.get('photo'):
            Avatar.objects.update_or_create(identity=actor.identity,
                defaults={'image': data['photo'], 'digest': hashlib.sha256(data['photo']).hexdigest()})
        if changes_audience or directory_changed:
            guard = lock_guard()
            guard.revision += 1
            guard.save(update_fields=['revision'])
            from accounts.models import AccessAudit
            AccessAudit.objects.create(user_id=actor.user_id, action='directory_visibility' if directory_changed else 'photo_visibility',
                revision=guard.revision, operator='profile-owner')
        if changed_email and pref.contact_email:
            from notifications.delivery import queue_verification
            queue_verification(pref)
        return pref


def request_verification(request, marker):
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        pref = Preferences.objects.select_for_update().filter(user_id=actor.user_id).first()
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        if not pref or not pref.contact_email or pref.email_verified_at:
            raise AccessDenied('not_found', 404)
        from notifications.models import EmailOutbox
        from notifications.delivery import queue_verification
        recent = EmailOutbox.objects.filter(user_id=actor.user_id, email_revision=pref.email_revision,
            purpose='verification', created_at__gt=actor.checked_at-timezone.timedelta(minutes=2)).exists()
        if not recent:
            queue_verification(pref)


def verify_email(request, marker, token):
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        try:
            payload = signing.loads(token, salt='notification-address', max_age=3600)
        except signing.BadSignature:
            raise AccessDenied('stale_context', 409)
        from notifications.models import EmailOutbox
        row = EmailOutbox.objects.select_for_update().filter(user_id=actor.user_id, key=payload.get('key'),
            purpose='verification', email_revision=payload.get('revision'), status__in=['accepted', 'captured']).first()
        pref = Preferences.objects.select_for_update().filter(user_id=actor.user_id).first()
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        if not row or not pref or row.email_revision != pref.email_revision or row.address != pref.contact_email:
            raise AccessDenied('not_found', 404)
        if not pref.email_verified_at:
            pref.email_verified_at = actor.checked_at
            pref.version += 1
            pref.save(update_fields=['email_verified_at', 'version'])
            row.confirmed_at = actor.checked_at
            row.save(update_fields=['confirmed_at'])


def save_resource(request, marker, data, resource_id=None):
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        groups = publisher_groups(actor)
        if not groups.filter(pk=data['group'].pk).exists():
            raise AccessDenied('not_found', 404)
        row = GroupResource.objects.select_for_update().filter(pk=resource_id, group__in=groups).first() if resource_id else None
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        if resource_id and not row:
            raise AccessDenied('not_found', 404)
        if data['version'] != (row.version if row else 0):
            raise AccessDenied('stale_context', 409)
        row = row or GroupResource(publisher=actor.identity, version=0)
        if row.pk and row.group_id != data['group'].pk:
            raise AccessDenied('immutable_group', 409)
        for field in ('group', 'category', 'title', 'description', 'url', 'available_from', 'available_until'):
            setattr(row, field, data[field])
        row.version += 1
        from .forms import https_link
        row.url = https_link(row.url)
        row.full_clean(exclude=['demo_key'])
        row.save()
        revision=GroupResourceRevision.objects.create(resource=row, version=row.version, actor=actor.identity,
            payload={field: (getattr(row, field).isoformat() if field.startswith('available_') else getattr(row, field))
                for field in ('group_id', 'category', 'title', 'description', 'url', 'available_from', 'available_until')})
        from workspaces.common import emit
        emit('resource',resource_revision=revision)
        return row.pk
