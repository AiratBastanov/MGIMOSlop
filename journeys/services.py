import hashlib
import json
import uuid
from django.core import signing
from django.db import transaction
from django.utils import timezone
from accounts.access import AccessDenied, current_actor, lock_guard
from materials.services import MaterialError, text, version
from student_requests.services import create_journey_request
from .models import Journey, JourneyRevision, JourneyOperation, JourneySubmission, JourneyBooking
from .templates import TEMPLATES, DOCUMENTS, NEEDS, PROGRAM, initial_state
from .selectors import journey_scope, record_options


def _digest(value):
    try:
        encoded=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
    except UnicodeEncodeError:
        raise MaterialError('Текст содержит недопустимый символ.')
    return hashlib.sha256(encoded).hexdigest()


def _bool(value):
    if type(value) is not bool: raise MaterialError('Нужна явная отметка: да или нет.')
    return value


def normalize_state(key,state):
    base=initial_state(key)
    if not isinstance(state,dict) or set(state)!=set(base): raise MaterialError('Откройте текущую форму подготовки.')
    checks=state['checks']
    if not isinstance(checks,dict) or set(checks)!=set(base['checks']): raise MaterialError('Эти шаги нельзя отмечать вручную.')
    base['checks']={k:_bool(v) for k,v in checks.items()}
    for name in ('questions','private_note','record','requested_change'):
        base[name]=text(state[name],field=name,limit=100 if name=='record' else 500)
    docs=state['documents']
    if not isinstance(docs,dict) or set(docs)!=set(base['documents']): raise MaterialError('Проверьте перечень документов.')
    for name,item in docs.items():
        if not isinstance(item,dict) or set(item)!={'need','source','prepared'} or not isinstance(item['need'],str) or item['need'] not in NEEDS: raise MaterialError('Выберите применимость документа.')
        source=text(item['source'],field='source',limit=250)
        if item['need']!='clarify' and not source: raise MaterialError('Укажите, на основании чего определена применимость документа.')
        base['documents'][name]={'need':item['need'],'source':source,'prepared':_bool(item['prepared'])}
    return base


def _operation(actor,data,journey_id):
    try: key=uuid.UUID(str(data.get('operation','')))
    except (ValueError,TypeError): raise MaterialError('Откройте форму заново: ключ действия отсутствует.',status=409)
    fingerprint=_digest({'journey':journey_id,'data':{k:v for k,v in data.items() if k not in ('context','csrfmiddlewaretoken','operation')}})
    old=JourneyOperation.objects.filter(user_id=actor.user_id,key=key).first()
    if old and old.fingerprint!=fingerprint: raise MaterialError('Этот ключ уже использован для другого действия.',status=409)
    if old and not journey_scope(actor).filter(pk=old.journey_id).exists(): raise AccessDenied('not_found',404)
    return key,fingerprint,old


def _record(actor,key,fingerprint,row,outcome):
    JourneyOperation.objects.create(user_id=actor.user_id,key=key,fingerprint=fingerprint,journey=row,outcome=outcome)
    return {**outcome,'marker':actor.marker,'state':'saved'}


def preparation_snapshot(actor,row,share_note=False):
    state=row.state
    template=TEMPLATES[(row.template_key,row.template_revision)]
    shared=state['private_note'] if share_note else ''
    if row.template_key=='exchange':
        return {'schema':1,'journey':row.pk,'journey_revision':row.version,'template_revision':row.template_revision,
            'program':PROGRAM,'source':template['source'],
            'checks':[{'label':step['title'],'marked_by_student':state['checks'][step['key']]} for step in template['steps'] if step['kind']=='personal'],
            'documents':[{'category':label, 'need':NEEDS[state['documents'][key]['need']],
                'source':state['documents'][key]['source'],'marked_by_student':state['documents'][key]['prepared']} for key,label in DOCUMENTS.items()],
            'questions':state['questions'],'shared_note':shared}
    options,_=record_options(actor,actor.checked_at)
    selected=next((r for r in options if r['key']==state['record']),None)
    if not selected: raise MaterialError('Выберите доступную актуальную учебную запись.')
    if not state['requested_change']: raise MaterialError('Опишите предполагаемую неточность.')
    return {'correction_area':selected['area'],'record_label':selected['label'][:160],
        'requested_change':state['requested_change'],'comment':shared,'record_url':selected['url'],
        'source_revision':selected['revision'],'journey_revision':row.version}


def validate_snapshot(snapshot):
    required={'schema','journey','journey_revision','template_revision','program','source','checks','documents','questions','shared_note'}
    if not isinstance(snapshot,dict) or set(snapshot)!=required or snapshot['schema']!=1 or snapshot['template_revision']!=1:
        raise MaterialError('Версия плана не поддерживается.')
    if snapshot['program']!=PROGRAM or len(json.dumps(snapshot,ensure_ascii=False))>10000:
        raise MaterialError('Проверьте снимок плана.')


def preview(request,marker,journey_id,expected_version,share_note):
    with transaction.atomic():
        guard=lock_guard();actor=current_actor(request,guard=guard,lock=True,expected=marker)
        row=journey_scope(actor).select_for_update().filter(pk=journey_id).first()
        if not row: raise AccessDenied('not_found',404)
        actor=current_actor(request,guard=guard,lock=True,expected=marker)
        if row.version!=version(expected_version): raise MaterialError('Подготовка уже изменена. Откройте сохранённую редакцию.',status=409)
        snapshot=preparation_snapshot(actor,row,_bool(share_note))
        token=signing.dumps({'user':actor.user_id,'marker':actor.marker,'journey':row.pk,'version':row.version,
            'share_note':share_note,'digest':_digest(snapshot)},salt='journey-preview-v1')
        return {'marker':actor.marker,'snapshot':snapshot,'token':token,'kind':row.template_key,'state':'preview'}


def perform(request,marker,data,journey_id=None):
    with transaction.atomic():
        guard=lock_guard();actor=current_actor(request,guard=guard,lock=True,expected=marker)
        if actor.role!='student': raise AccessDenied('forbidden',403)
        key,digest,old=_operation(actor,data,journey_id)
        if old: return {**old.outcome,'marker':actor.marker,'state':'saved','replayed':True}
        action=data.get('action')
        if action=='start' and journey_id is None:
            template=data.get('template')
            if not isinstance(template,str) or (template,1) not in TEMPLATES: raise MaterialError('Выберите один из предложенных вопросов.')
            row=journey_scope(actor).filter(template_key=template,template_revision=1).first()
            if not row:
                row=Journey.objects.create(owner_id=actor.profile_id,template_key=template,state=initial_state(template))
                JourneyRevision.objects.create(journey=row,version=1,state=row.state)
            return _record(actor,key,digest,row,{'id':row.pk,'url':f'/journeys/{row.pk}/','version':row.version})
        row=journey_scope(actor).select_for_update().filter(pk=journey_id).first()
        if not row: raise AccessDenied('not_found',404)
        actor=current_actor(request,guard=guard,lock=True,expected=marker)
        if not journey_scope(actor).filter(pk=row.pk).exists(): raise AccessDenied('not_found',404)
        if row.version!=version(data.get('version')): raise MaterialError('В другой вкладке уже сохранены изменения. Обновите подготовку перед новым действием.',status=409)
        outcome={'id':row.pk,'url':f'/journeys/{row.pk}/','version':row.version}
        if action=='save':
            state=normalize_state(row.template_key,data.get('state'))
            if state!=row.state:
                row.state=state;row.version+=1;row.save(update_fields=['state','version','updated_at'])
                JourneyRevision.objects.create(journey=row,version=row.version,state=state)
            outcome['version']=row.version
        elif action=='submit':
            try: proof=signing.loads(data.get('preview_token',''),salt='journey-preview-v1')
            except (signing.BadSignature,TypeError): raise MaterialError('Откройте предпросмотр перед отправкой.',status=409)
            if any(proof.get(k)!=v for k,v in {'user':actor.user_id,'marker':actor.marker,'journey':row.pk,'version':row.version}.items()):
                raise MaterialError('Предпросмотр устарел. Снова проверьте отправляемые данные.',status=409)
            snapshot=preparation_snapshot(actor,row,_bool(proof.get('share_note')))
            if proof.get('digest')!=_digest(snapshot): raise MaterialError('Учебный контекст изменился. Повторите предпросмотр.',status=409)
            latest=row.submissions.select_related('request').order_by('-pk').first()
            if latest and latest.request.status in ('submitted','in_progress'):
                raise MaterialError('Предыдущая заявка ещё рассматривается. Ответьте на уточнение в ней.',status=409)
            if row.template_key=='exchange':
                req=create_journey_request(actor=actor,key=key,snapshot=snapshot)
            else:
                req=create_journey_request(actor=actor,key=key,correction={k:snapshot[k] for k in ('correction_area','record_label','requested_change')},comment=snapshot['comment'])
            JourneySubmission.objects.create(journey=row,revision=row.revisions.get(version=row.version),request=req)
            outcome.update(request_id=req.pk,request_url=f'/requests/{req.pk}/')
        elif action=='link_booking':
            from consultations.selectors import booking_scope
            value=data.get('booking')
            if type(value) is not int or not 1<=value<=2147483647: raise MaterialError('Выберите свою сохранённую запись.')
            booking=booking_scope(actor,actor.checked_at).filter(pk=value).first()
            if not booking: raise AccessDenied('not_found',404)
            JourneyBooking.objects.get_or_create(journey=row,booking=booking)
        else: raise MaterialError('Выберите допустимое действие.')
        return _record(actor,key,digest,row,outcome)
