from datetime import timedelta
from urllib.parse import urlencode
from django.core import signing
from django.core.paginator import Paginator
from django.db.models import Q, Exists, OuterRef, Subquery, F, Case, When, Value, CharField, Window
from django.db.models.functions import Cast, Concat, Coalesce, RowNumber, Least, Greatest
from .models import NotificationReceipt, NotificationRead, InboxClear


def inbox_events(actor, *, now):
    return presentation_events(actor,now=now,mode='inbox')


def presentation_events(actor, *, now, mode='inbox'):
    """Select the latest accessible thread event BEFORE applying personal state.

    No GET writes, no worker dependency. Legacy reads retain their original time.
    Restoration extends personal history visibility, never recipient availability.
    """
    from accounts.selectors import domain_notifications
    eligible_receipt=NotificationReceipt.objects.filter(user_id=actor.user_id,notification_id=OuterRef('pk'),available_at__lte=now)
    all_events=domain_notifications(actor,now=now,archive=True).annotate(has_receipt=Exists(eligible_receipt)).filter(has_receipt=True)
    latest=thread_events(all_events,actor).values('pk')
    receipt=NotificationReceipt.objects.filter(user_id=actor.user_id,notification_id=OuterRef('pk'))
    reads=NotificationRead.objects.filter(user_id=actor.user_id,notification_id=OuterRef('pk'))
    query=(all_events.filter(pk__in=Subquery(latest)) if mode=='inbox' else all_events).annotate(
        available_at=Coalesce(Subquery(receipt.values('available_at')[:1]),F('published_at')),
        dismissed_at=Subquery(receipt.values('dismissed_at')[:1]),
        restored_at=Subquery(receipt.values('restored_at')[:1]),
        read_at=Subquery(reads.values('read_at')[:1]),
        current_event=Exists(domain_notifications(actor,now=now).filter(pk=OuterRef('pk'))),
    ).annotate(natural_history_at=Least(F('available_at')+timedelta(days=7),Coalesce(F('read_at')+timedelta(hours=24),F('available_at')+timedelta(days=7))))
    query=query.annotate(history_at=Case(When(current_event=False,restored_at__isnull=False,then=Least(F('natural_history_at'),F('restored_at'))),default=F('natural_history_at')))
    query=query.annotate(history_until=Greatest(F('history_at'),Coalesce(F('restored_at'),F('history_at')))+timedelta(days=30))
    if mode=='trash':
        return query.filter(available_at__lte=now,dismissed_at__gt=now-timedelta(days=7),dismissed_at__lte=now)
    query=query.filter(available_at__lte=now,dismissed_at__isnull=True)
    if mode=='history': return query.filter(history_at__lte=now,history_until__gt=now)
    return query.filter(history_at__gt=now,current_event=True)


def thread_events(query, actor):
    receipt = NotificationReceipt.objects.filter(user_id=actor.user_id, notification_id=OuterRef('pk'))
    targets = [('application_event__consultation_event__booking_id','consultation-booking'), ('application_event__consultation_event__slot_id','consultation-slot'), ('application_event__teaching_revision__publication_id','teaching'), ('application_event__submission__material_id','submission'), ('application_event__review__revision__material_id','review'), ('application_event__clarification_message__clarification__request_id','clarification'), ('application_event__notice_revision__notice_id','notice'), ('application_event__resource_revision__resource_id','resource'), ('request_event__request_id', 'request'), ('schedule_change__lesson_id', 'lesson'),
        ('announcement_change__announcement_id', 'announcement'), ('final_result_change__result__enrollment_id', 'result'),
        ('requirement_change__requirement_id', 'requirement'), ('assessment_change__assessment_id', 'assessment')]
    key = Case(*[When(**{field + '__isnull': False}, then=Concat(Value(prefix+':'), Cast(field, CharField())))
        for field, prefix in targets], default=Concat(Value('event:'), Cast('pk', CharField())), output_field=CharField())
    return query.annotate(thread_key=key,
        available_at=Coalesce(Subquery(receipt.values('available_at')[:1]), F('published_at')),
        is_read=Exists(NotificationRead.objects.filter(user_id=actor.user_id, notification_id=OuterRef('pk')))
    ).annotate(thread_row=Window(RowNumber(), partition_by=[F('thread_key')],
        order_by=[F('available_at').desc(), F('published_at').desc(), F('pk').desc()])).filter(thread_row=1)


def inbox_unread(actor, now):
    return inbox_events(actor, now=now).annotate(
        read=Exists(NotificationRead.objects.filter(user_id=actor.user_id, notification_id=OuterRef('pk')))).filter(read=False).count()


def inbox_page(actor, *, now, kind='all', archive=False, page_number=1, notification_id=None, mode=None):
    from accounts.selectors import domain_notifications
    mode=mode or ('history' if archive else 'inbox')
    query = presentation_events(actor,now=now,mode=mode)
    if notification_id:
        query = query.filter(pk=notification_id)
    if kind == 'requests':
        query = query.filter(Q(request_event__isnull=False)|Q(application_event__kind='clarification'))
    elif kind == 'schedule':
        query = query.filter(Q(schedule_change__isnull=False)|Q(application_event__kind='consultation'))
    elif kind == 'academic':
        query = query.filter(Q(final_result_change__isnull=False)|Q(requirement_change__isnull=False)|Q(assessment_change__isnull=False)|Q(application_event__kind__in=['teaching','submission','review']))
    elif kind in ('announcements', 'deadlines'):
        query = query.filter(Q(announcement_change__isnull=False)|Q(application_event__kind__in=['notice','resource']))
        if kind == 'deadlines':
            query = query.filter(Q(announcement_change__announcement__due_at__isnull=False)|Q(application_event__notice_revision__payload__due_at__isnull=False))
    # Inbox already selected the latest authorized event of each thread before
    # applying personal state. A second window over those rows is redundant.
    query = (query.annotate(is_read=Exists(NotificationRead.objects.filter(user_id=actor.user_id,notification_id=OuterRef('pk')))) if mode in ('trash','inbox') else thread_events(query, actor)).select_related('schedule_change', 'announcement_change__announcement',
        'final_result_change__result', 'requirement_change__requirement', 'assessment_change__assessment', 'request_event__request', 'application_event'
    ).order_by('-available_at', '-published_at', '-pk')
    page = Paginator(query, 10).get_page(page_number)
    def url(number):
        return '/notifications/?' + urlencode({'kind': kind, 'mode': mode, 'page': number})
    cutoff = NotificationReceipt.objects.filter(user_id=actor.user_id).order_by('-pk').values_list('pk', flat=True).first() or 0
    clear_token = signing.dumps({'user': actor.user_id, 'marker': actor.marker, 'receipt_max': cutoff,
        'at': now.isoformat()}, salt='inbox-clear')
    from .inbox_actions import snapshot_token
    if mode == 'inbox':
        # This exact authorized snapshot supplies both unread and the signed
        # read-all operation; late events are still outside the token.
        active = list(inbox_events(actor,now=now).annotate(read=Exists(
            NotificationRead.objects.filter(user_id=actor.user_id,notification_id=OuterRef('pk')))).values_list('pk','read'))
        unread = sum(not read for _,read in active)
        read_all_token = snapshot_token(actor,[pk for pk,_ in active],'read-all')
    else:
        unread = inbox_unread(actor,now)
        read_all_token = ''
    return {'page_obj': page, 'events': list(page.object_list), 'notification_count': page.paginator.count,
        'inbox_mode':mode,'page_notice':'Такой страницы нет. Показана последняя доступная страница.' if str(page_number)!=str(page.number) else '',
        'read_all_token':read_all_token,'unread':unread,
        'page_previous_url': url(page.previous_page_number()) if page.has_previous() else '',
        'page_next_url': url(page.next_page_number()) if page.has_next() else '', 'clear_token': clear_token}
