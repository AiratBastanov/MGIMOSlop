"""Application-owned targets composed into the existing recipient inbox."""
from datetime import datetime
from django.db.models import Q, F
from accounts.access import AccessDenied
from materials.selectors import material_scope, assignment_scope
from student_requests.selectors import request_scope
from profiles.selectors import group_resources
from .models import ApplicationEvent
from .selectors import teaching_scope, notice_scope, review_result


def event_scope(actor,now,archive=False):
    allowed=Q(pk__in=[])
    if actor.role=='student':
        teaching=Q(teaching_revision__publication__in=teaching_scope(actor,now))
        if not archive: teaching &= Q(teaching_revision_id=F('teaching_revision__publication__published_revision_id'))
        notices=Q(notice_revision__notice__in=notice_scope(actor,now,archive=archive))
        if not archive: notices &= Q(notice_revision_id=F('notice_revision__notice__published_revision_id'))
        reviews=Q(review__revision__material__owner_id=actor.profile_id)
        clarification=Q(clarification_message__clarification__request__in=request_scope(actor),clarification_message__action__in=['ask','resolve','close'])
        resources=Q(resource_revision__resource__in=group_resources(actor,now))
        if not archive: resources &= Q(resource_revision__version=F('resource_revision__resource__version'))
        allowed=teaching|notices|reviews|clarification|resources
    elif actor.role=='teacher':
        allowed=Q(submission__material__in=material_scope(actor,now))
    elif actor.role=='staff':
        allowed=Q(clarification_message__clarification__request__in=request_scope(actor),clarification_message__action='reply')
    else: raise AccessDenied('forbidden',403)
    return ApplicationEvent.objects.filter(allowed)


def values(notification,now):
    e=notification.application_event
    base={'id':notification.pk,'kind':'application','event_id':f'application:{e.pk}',
        'published_at':e.created_at,'is_read':notification.is_read,'available':True,'can_read':True,
        'read_url':f'/notifications/{notification.pk}/read/','detail_url':f'/notifications/{notification.pk}/',
        'source_message':'','body':'','is_historical':False,'status_label':'','action_label':'Открыть','history_url':'','deadline_label':''}
    if e.teaching_revision_id:
        rev=e.teaching_revision;row=rev.publication
        base.update(title=('Задание' if row.kind=='task' else 'Материал преподавателя')+': '+rev.payload['title'],
            body='Версия '+str(rev.number)+'. '+rev.payload.get('topic',''),target_url=f'/study/teaching/{row.pk}/',
            is_historical=row.published_revision_id!=rev.pk)
    elif e.submission_id:
        rev=e.submission
        base.update(title='Работа поступила на проверку',body=f'{rev.material.owner.name} · {rev.title} · версия {rev.number}',
            target_url=f'/teacher/materials/{rev.material_id}/#revision-{rev.pk}',is_historical=rev.number!=rev.material.submission_count)
    elif e.review_id:
        rev=e.review.revision
        base.update(title='Отзыв к работе: '+rev.title,body=f'Версия {rev.number}. '+e.review.get_status_display()+'. Результат работы: '+review_result(e.review.result),
            target_url=f'/study/materials/{rev.material_id}/#revision-{rev.pk}',is_historical=rev.number!=rev.material.submission_count)
    elif e.clarification_message_id:
        message=e.clarification_message;request=message.clarification.request
        student=message.action!='reply'
        base.update(title={'ask':'Нужно уточнение по заявке','reply':'Получен ответ студента','resolve':'Уточнение рассмотрено','close':'Уточнение закрыто'}[message.action],
            body=request.reference+' · '+message.body,target_url=f"/{'requests' if student else 'staff/requests'}/{request.pk}/#clarification")
    elif e.notice_revision_id:
        rev=e.notice_revision;row=rev.notice
        until=rev.payload.get('visible_until');expired=bool(until and datetime.fromisoformat(until)<=now)
        withdrawn=row.state=='withdrawn'
        base.update(title='Объявление службы: '+rev.payload['title'],body='' if withdrawn else rev.payload['body'],
            target_url=f'/notices/{row.pk}/',is_historical=rev.pk!=row.published_revision_id,
            status_label='Объявление отозвано' if withdrawn else 'Срок показа завершён' if expired else 'Опубликовано')
    else:
        rev=e.resource_revision
        base.update(title='Ресурс группы: '+rev.payload['title'],body=rev.payload['description'],target_url='/profile/group/',
            is_historical=rev.version!=rev.resource.version)
    return base


def inbox(actor,now,kind='all',page_number=1,notification_id=None,mode='inbox'):
    from notifications.selectors import inbox_page
    from notifications.inbox_actions import snapshot_token
    page=inbox_page(actor,now=now,kind=kind,page_number=page_number,notification_id=notification_id,mode=mode)
    events=page.pop('events')
    if notification_id and not events: raise AccessDenied('not_found',404)
    from accounts.selectors import notification_values
    rows=[]
    for event in events:
        if event.application_event_id: item=values(event,now)
        elif event.request_event_id:
            item=notification_values(event,now,{},None)
            if actor.role=='staff': item['target_url']=f'/staff/requests/{event.request_event.request_id}/';item['history_url']=item['target_url']+'#request-history'
        else: continue
        item.update(can_read=True,recipient_token=snapshot_token(actor,[event.pk],'trash'));rows.append(item)
    return {**page,'notifications':rows,'archive_mode':mode=='history','notification_kind':kind,'notification_filter_label':'',
        'notification_only_requests':True,'section':'notifications'}
