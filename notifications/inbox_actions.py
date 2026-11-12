from datetime import datetime, timedelta
import uuid
from django.core import signing
from django.db import transaction
from django.utils import timezone
from accounts.access import current_actor, lock_guard, AccessDenied
from .models import NotificationReceipt, InboxClear
from .selectors import inbox_events

class InboxConflict(Exception):
    """The context is valid, but this recipient action is no longer available."""

def snapshot_token(actor, ids, purpose, **extra):
    if purpose in ('trash','restore') and len(ids)==1:
        receipt=NotificationReceipt.objects.filter(user_id=actor.user_id,notification_id=ids[0]).first()
        extra.update(dismissed=receipt.dismissed_at.isoformat() if receipt and receipt.dismissed_at else None,
                     restored=receipt.restored_at.isoformat() if receipt and receipt.restored_at else None)
    return signing.dumps({'user':actor.user_id,'marker':actor.marker,'ids':ids,**extra},salt='recipient-'+purpose,compress=True)



def clear_inbox(request, marker, token):
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        try:
            data = signing.loads(token, salt='inbox-clear', max_age=7200)
            cutoff = datetime.fromisoformat(data['at'])
            if data['user'] != actor.user_id or data['marker'] != actor.marker or timezone.is_naive(cutoff):
                raise ValueError()
        except (signing.BadSignature, ValueError, KeyError, TypeError):
            raise AccessDenied('stale_context', 409)
        # Same signed snapshot is one clear operation, including a lost HTTP response.
        # Historical random batch IDs remain valid; no migration or event rewrite.
        key = uuid.uuid5(uuid.NAMESPACE_URL, 'deanery:inbox-clear:' + token)
        batch, created = InboxClear.objects.get_or_create(pk=key,
            defaults={'user_id':actor.user_id, 'cutoff':cutoff})
        if not created:
            if batch.user_id != actor.user_id or batch.cutoff != cutoff:
                raise AccessDenied('stale_context',409)
            return batch.pk
        NotificationReceipt.objects.filter(user_id=actor.user_id, pk__lte=data['receipt_max'],
            available_at__lte=cutoff, notification__in=inbox_events(actor, now=actor.checked_at)
        ).update(dismissed_at=actor.checked_at, clear_batch=batch)
        return batch.pk


def undo_clear(request, marker, batch_id):
    with transaction.atomic():
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        batch = InboxClear.objects.select_for_update().filter(pk=batch_id, user_id=actor.user_id).first()
        if not batch:
            raise AccessDenied('not_found', 404)
        if batch.undone_at is not None:
            return
        if batch.created_at <= actor.checked_at-timedelta(days=7):
            raise InboxConflict('Срок восстановления истёк. История объектов по-прежнему доступна.')
        actor = current_actor(request, guard=lock_guard(), lock=True, expected=marker)
        from accounts.selectors import domain_notifications
        NotificationReceipt.objects.filter(user_id=actor.user_id, clear_batch=batch,
            dismissed_at__gt=actor.checked_at-timedelta(days=7), available_at__lte=actor.checked_at,
            notification__in=domain_notifications(actor, now=actor.checked_at,archive=True)
        ).update(dismissed_at=None, clear_batch=None,restored_at=actor.checked_at)
        batch.undone_at = actor.checked_at
        batch.save(update_fields=['undone_at'])
