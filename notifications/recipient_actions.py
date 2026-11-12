"""Explicit recipient operations; immutable domain events are never removed."""
from django.core import signing
from django.db import transaction
from accounts.access import current_actor, lock_guard, AccessDenied
from accounts.selectors import domain_notifications
from student_requests.selectors import request_scope
from .models import NotificationRead, NotificationReceipt
from .inbox_actions import InboxConflict, snapshot_token
from .selectors import presentation_events, inbox_unread

def decode(actor, token, purpose):
    try:
        data=signing.loads(token,salt='recipient-'+purpose,max_age=7200)
        if data['user']!=actor.user_id or data['marker']!=actor.marker or not isinstance(data['ids'],list) or len(data['ids'])>10000 or any(type(i)!=int for i in data['ids']): raise ValueError()
        return data
    except (signing.BadSignature,ValueError,KeyError,TypeError):
        raise InboxConflict('Снимок действия устарел. Обновите страницу и повторите действие.')

def record_reads(actor, ids):
    # Exact IDs, scoped again at mutation time; repeated POST preserves read_at.
    for pk in domain_notifications(actor,now=actor.checked_at,archive=True).filter(pk__in=ids).values_list('pk',flat=True):
        NotificationRead.objects.get_or_create(user_id=actor.user_id,notification_id=pk)

def read_all(request, marker, token):
    with transaction.atomic():
        actor=current_actor(request,guard=lock_guard(),lock=True,expected=marker)
        data=decode(actor,token,'read-all')
        record_reads(actor,data['ids'])
        return {'marker':actor.marker,'unread':inbox_unread(actor,actor.checked_at)}

def viewed_token(actor, row):
    receipts=NotificationReceipt.objects.filter(user_id=actor.user_id,available_at__lte=actor.checked_at).values('notification_id')
    ids=list(domain_notifications(actor,now=actor.checked_at,archive=True).filter(pk__in=receipts,request_event__request_id=row.pk,request_event__version__lte=row.version).values_list('pk',flat=True))
    return snapshot_token(actor,ids,'viewed-request',request=row.pk,version=row.version)

def acknowledge_request(request, marker, token):
    with transaction.atomic():
        actor=current_actor(request,guard=lock_guard(),lock=True,expected=marker)
        data=decode(actor,token,'viewed-request')
        if actor.role!='student' or not request_scope(actor).filter(pk=data.get('request')).exists(): raise AccessDenied('not_found',404)
        ids=domain_notifications(actor,now=actor.checked_at,archive=True).filter(pk__in=data['ids'],request_event__request_id=data['request'],request_event__version__lte=data['version']).values_list('pk',flat=True)
        record_reads(actor,list(ids))
        return {'marker':actor.marker,'unread':inbox_unread(actor,actor.checked_at)}

def trash_event(request, marker, token, restore=False):
    with transaction.atomic():
        actor=current_actor(request,guard=lock_guard(),lock=True,expected=marker)
        data=decode(actor,token,'restore' if restore else 'trash')
        if len(data['ids'])!=1: raise InboxConflict('Выберите одно уведомление.')
        pk=data['ids'][0]
        if not domain_notifications(actor,now=actor.checked_at,archive=True).filter(pk=pk).exists(): raise AccessDenied('not_found',404)
        receipt=NotificationReceipt.objects.select_for_update().filter(user_id=actor.user_id,notification_id=pk).first()
        if not receipt: raise InboxConflict('Уведомление недоступно. Обновите ленту.')
        actor=current_actor(request,guard=lock_guard(),lock=True,expected=marker)
        state={'dismissed':receipt.dismissed_at.isoformat() if receipt.dismissed_at else None,
               'restored':receipt.restored_at.isoformat() if receipt.restored_at else None}
        if restore:
            if receipt.dismissed_at is None: return {'marker':actor.marker}
            if state['dismissed']!=data.get('dismissed') or state['restored']!=data.get('restored'):
                raise InboxConflict('Уведомление изменилось в другой вкладке. Обновите корзину.')
            if not presentation_events(actor,now=actor.checked_at,mode='trash').filter(pk=pk).exists():
                raise InboxConflict('Срок восстановления истёк. История самого объекта сохранена.')
            receipt.dismissed_at=None;receipt.clear_batch=None;receipt.restored_at=actor.checked_at
            receipt.save(update_fields=['dismissed_at','clear_batch','restored_at'])
        elif state['restored']!=data.get('restored'):
            raise InboxConflict('Уведомление уже восстановлено в другой вкладке. Обновите ленту.')
        elif receipt.dismissed_at is None:
            eligible=presentation_events(actor,now=actor.checked_at).filter(pk=pk).exists() or presentation_events(actor,now=actor.checked_at,mode='history').filter(pk=pk).exists()
            if not eligible: raise InboxConflict('Уведомление уже вышло из срока показа. Обновите ленту.')
            receipt.dismissed_at=actor.checked_at
            receipt.save(update_fields=['dismissed_at'])
        return {'marker':actor.marker,'unread':inbox_unread(actor,actor.checked_at)}
