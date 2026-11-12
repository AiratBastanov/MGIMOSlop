from django.db import transaction
from accounts.access import lock_guard, current_actor, AccessDenied
from accounts.selectors import visible_notifications
from .models import NotificationRead
from .selectors import presentation_events

def readable(actor):
    from accounts.selectors import domain_notifications
    ids=presentation_events(actor,now=actor.checked_at).values('pk')
    archived=presentation_events(actor,now=actor.checked_at,mode='history').values('pk')
    from django.db.models import Q, Subquery
    return domain_notifications(actor,now=actor.checked_at,archive=True).filter(Q(pk__in=Subquery(ids))|Q(pk__in=Subquery(archived)))


def mark_read(request, notification_id, marker):
    with transaction.atomic():
        guard = lock_guard()
        actor = current_actor(request,guard=guard,lock=True,expected=marker)
        target = readable(actor).select_for_update(of=('self',)).filter(pk=notification_id).first()
        if target is None: raise AccessDenied('not_found',404)
        # Deadline мог наступить во время ожидания target lock: решение о записи
        # принимается после всех locks, даже если входной контекст был действующим.
        actor = current_actor(request,guard=guard,lock=True,expected=marker)
        # Пока ожидали lock, мог завершиться срок публикации/действия набора.
        if not readable(actor).filter(pk=target.pk).exists():
            raise AccessDenied('not_found',404)
        row,created = NotificationRead.objects.get_or_create(user_id=actor.user_id,notification=target)
        return {'created':created,'marker':actor.marker}
