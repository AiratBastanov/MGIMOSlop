"""Scoped публикации службы и короткий обмен уточнениями под lock заявки."""
from datetime import datetime
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from accounts.access import current_actor, lock_guard, AccessDenied
from accounts.models import StaffScope
from materials.services import MaterialError, text, version, role
from profiles.selectors import publisher_groups
from student_requests.models import Request
from student_requests.selectors import request_scope
from student_requests.workflow import TERMINAL
from .models import StaffNotice, StaffNoticeRevision, Clarification, ClarificationMessage
from .common import instant, https_link, operation, record_operation, emit
from .selectors import notice_scope


def notice_payload(data):
    return {'title':text(data.get('title',''),field='title',limit=180,required=True),
        'body':text(data.get('body',''),field='body',limit=4000),
        'link':https_link(data.get('link','')),
        'visible_until':instant(data.get('visible_until',''),'visible_until'),
        'due_at':instant(data.get('due_at',''),'due_at')}


def save_notice(request,marker,data,notice_id=None):
    with transaction.atomic():
        guard=lock_guard();actor=current_actor(request,guard=guard,lock=True,expected=marker);role(actor,'staff')
        row=notice_scope(actor,actor.checked_at).select_for_update(of=('self',)).filter(pk=notice_id).first() if notice_id else None
        if notice_id and not row: raise AccessDenied('not_found',404)
        actor=current_actor(request,guard=guard,lock=True,expected=marker)
        if row and not notice_scope(actor,actor.checked_at).filter(pk=row.pk).exists(): raise AccessDenied('not_found',404)
        key,digest,retry=operation(actor,data,target=['notice',notice_id])
        if retry:
            if not notice_scope(actor,actor.checked_at).filter(pk=retry['id']).exists(): raise AccessDenied('not_found',404)
            return {**retry,'marker':actor.marker,'repeated':True}
        action=data.get('action')
        if action not in ('save','publish','withdraw'): raise MaterialError('Выберите действие объявления.')
        group=data.get('group','')
        if group and (not str(group).isdigit() or not publisher_groups(actor).filter(pk=group).exists()): raise AccessDenied('not_found',404)
        if row:
            if row.version!=version(data.get('expected_version')): raise MaterialError('Объявление уже изменилось. Сравните с сохранённой версией.',status=409)
            if str(row.group_id or '')!=str(group): raise MaterialError('Аудитория закреплена. Для другой группы создайте отдельное объявление.',status=409)
        else:
            if action=='withdraw': raise MaterialError('Сначала создайте объявление.')
            row=StaffNotice.objects.create(faculty_id=StaffScope.objects.get(identity=actor.identity).faculty_id,group_id=group or None,author=actor.identity)
        actor=current_actor(request,guard=guard,lock=True,expected=marker)
        if not notice_scope(actor,actor.checked_at).filter(pk=row.pk).exists(): raise AccessDenied('not_found',404)
        p=dict(row.published_revision.payload) if action=='withdraw' and row.published_revision_id else notice_payload(data)
        if action in ('save','publish'): row.draft=p
        if action=='publish':
            if not p['body']: raise MaterialError('Добавьте текст объявления.',field='body')
            for name in ('visible_until','due_at'):
                if p[name] and datetime.fromisoformat(p[name])<=actor.checked_at:
                    raise MaterialError('Укажите будущую дату. Публикация выполняется сразу.',field=name)
        if action!='save':
            if action=='withdraw' and not row.published_revision_id: raise MaterialError('Нет опубликованной версии.',status=409)
            state='withdrawn' if action=='withdraw' else 'published'
            rev=StaffNoticeRevision.objects.create(notice=row,number=(row.revisions.aggregate(n=Max('number'))['n'] or 0)+1,payload=p,state=state,author=actor.identity)
            row.published_revision=rev;row.state=state
        row.version+=1;row.save()
        if action!='save': emit('notice',notice_revision=rev)
        return record_operation(actor,key,digest,{'id':row.pk,'state':row.state})


def clarify(request,marker,request_id,data):
    with transaction.atomic():
        guard=lock_guard();actor=current_actor(request,guard=guard,lock=True,expected=marker)
        if actor.role not in ('staff','student'): raise AccessDenied('forbidden',403)
        row=request_scope(actor).select_for_update(of=('self',)).filter(pk=request_id).first()
        if not row: raise AccessDenied('not_found',404)
        actor=current_actor(request,guard=guard,lock=True,expected=marker)
        if not request_scope(actor).filter(pk=row.pk).exists(): raise AccessDenied('not_found',404)
        key,digest,retry=operation(actor,data,target=['clarification',request_id])
        if retry: return {**retry,'marker':actor.marker,'repeated':True}
        if row.status in TERMINAL: raise MaterialError('Заявка уже завершена. Новое уточнение или ответ недоступны.',status=409)
        action=data.get('action')
        pending=Clarification.objects.select_for_update().filter(request=row,state__in=['open','answered']).first()
        if action=='ask':
            role(actor,'staff')
            if pending: raise MaterialError('Сначала завершите текущее уточнение.',status=409)
            if row.version!=version(data.get('request_version')): raise MaterialError('Статус заявки изменился. Проверьте его перед вопросом.',status=409)
            body=text(data.get('body',''),field='body',limit=2000,required=True)
            pending=Clarification.objects.create(request=row)
            message=ClarificationMessage.objects.create(clarification=pending,number=1,action='ask',body=body,actor_id=actor.user_id)
        else:
            if not pending or str(pending.pk)!=str(data.get('clarification')) or pending.version!=version(data.get('expected_version')):
                raise MaterialError('Уточнение уже изменилось. Текущий ввод не отправлен.',status=409)
            if action=='reply':
                role(actor,'student')
                if pending.state!='open': raise MaterialError('Ответ уже сохранён.',status=409)
                body=text(data.get('body',''),field='body',limit=2000,required=True);pending.state='answered'
            elif action=='resolve':
                role(actor,'staff')
                if pending.state!='answered': raise MaterialError('Сначала дождитесь ответа студента.',status=409)
                body='Уточнение рассмотрено. Решение по заявке принимается отдельно.';pending.state='resolved'
            else: raise MaterialError('Выберите действие уточнения.')
            pending.version+=1;pending.save(update_fields=['state','version'])
            message=ClarificationMessage.objects.create(clarification=pending,number=pending.version,action=action,body=body,actor_id=actor.user_id)
        emit('clarification',clarification_message=message)
        return record_operation(actor,key,digest,{'id':row.pk,'clarification':pending.pk,'version':pending.version,'state':pending.state})


def terminal_clarifications(row,action,actor_id):
    """Вызывается только существующим request writer под parent lock."""
    from student_requests.services import RequestError
    pending=Clarification.objects.select_for_update().filter(request=row,state__in=['open','answered']).first()
    if action=='complete' and pending and pending.state=='open':
        raise RequestError('clarification_pending',status=409)
    if action in ('complete','reject') and pending:
        pending.state='resolved';pending.version+=1;pending.save(update_fields=['state','version'])
        message=ClarificationMessage.objects.create(clarification=pending,number=pending.version,action='close',
            body='Обмен закрыт решением по заявке. Сохранённые сообщения остаются в истории.',actor_id=actor_id)
        emit('clarification',clarification_message=message)
