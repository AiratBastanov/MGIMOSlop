from functools import wraps
import json
from django.db import DatabaseError
from django.http import JsonResponse
from django.shortcuts import redirect,render
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_GET,require_http_methods
from django.views.decorators.debug import sensitive_post_parameters
from accounts.access import AccessDenied,current_actor
from accounts.views import protected
from accounts.selectors import private_snapshot
from . import selectors,services
from .forms import DraftForm,PolicyForm,ReviewForm


def snapshot(request, *, material_id=None, assignment_id=None, teacher=False, expected_marker=None, editor_only=False):
    def extension(actor,now):
        services.role(actor,'teacher' if teacher else 'student')
        extra={'section':'teacher' if teacher else 'materials'}
        if material_id is not None:
            extra['material']=selectors.material_values(actor,material_id,now)
            from workspaces.views import event_view
            extra['viewed_events']=event_view(actor,now,**(
                {'application_event__submission__material_id':material_id} if teacher else
                {'application_event__review__revision__material_id':material_id}))
        elif assignment_id is not None:
            assignment=selectors.assignment_scope(actor,now).select_related('course','group','period','teacher__instructor').filter(pk=assignment_id).first()
            if assignment is None: raise AccessDenied('not_found',404)
            extra['assignment']=selectors.assignment_values(assignment,actor)
        elif editor_only:
            extra['assignments']=selectors.assignment_choices(actor,now)
        else:
            extra.update(selectors.workspace(actor,now))
        return extra
    data=private_snapshot(request,extension=extension)
    expected=expected_marker if expected_marker is not None else request.POST.get('context','')
    if (request.method=='POST' or expected_marker is not None) and not constant_time_compare(data['marker'],expected):
        raise AccessDenied('stale_context',409)
    return data


def page(request,template,data,error=None):
    if error: data['material_error']=error.message
    response=render(request,template,data,status=error.status if error else 200)
    response['X-Page-Context']=data['marker']
    return response


def saved(request,result,url):
    current_actor(request,expected=result['marker'])
    return redirect(url)


def form_error(form,error):
    if error:
        form.is_valid()
        form.add_error(error.field if error.field in form.fields else None,error.message)
    return form


@require_GET
@protected
def student_list(request):
    return page(request,'materials/list.html',snapshot(request))


@require_GET
@protected
def teacher_workspace(request):
    return page(request,'materials/teacher.html',snapshot(request,teacher=True))


@require_http_methods(['GET','POST'])
@sensitive_post_parameters('content','context')
@protected
def new(request):
    error=None
    if request.method=='POST':
        try:
            result=services.save_draft(request,request.POST.get('context',''),request.POST)
            return saved(request,result,f"/study/materials/{result['id']}/")
        except services.MaterialError as exc: error=exc
    data=snapshot(request)
    data['form']=form_error(DraftForm(request.POST if request.method=='POST' else None,assignments=data['assignments']),error)
    return page(request,'materials/new.html',data,error)


@require_http_methods(['GET','POST'])
@sensitive_post_parameters('content','context')
@protected
def detail(request,material_id):
    error=None
    action=request.POST.get('action','')
    if request.method=='POST':
        try:
            if action=='save':
                result=services.save_draft(request,request.POST.get('context',''),request.POST,material_id)
            elif action=='submit':
                result=services.submit_material(request,request.POST.get('context',''),material_id,
                    request.POST.get('expected_version'),request.POST.get('expected_policy'),request.POST)
            elif action=='correct':
                result=services.start_correction(request,request.POST.get('context',''),material_id,request.POST.get('expected_version'))
            else:
                # Сначала авторизация; неизвестное действие не становится writer.
                current_actor(request,expected=request.POST.get('context',''))
                raise services.MaterialError('Неизвестное действие.')
            return saved(request,result,f'/study/materials/{material_id}/')
        except services.MaterialError as exc: error=exc
    data=snapshot(request,material_id=material_id)
    item=data['material']
    if error and action=='save' and str(item['draft_version'])!=request.POST.get('expected_version'):
        error=services.MaterialError('Черновик изменился. Проверьте сохранённую версию перед редактированием.',status=409)
    initial={'title':item['draft_title'],'mode':item['draft_mode'],'content':item['draft_content']}
    bound=request.POST if error and action in ('save','submit') else None
    data['form']=form_error(DraftForm(bound,initial=initial,editing=True,policy=item['assignment']['policy']),error if bound is not None else None)
    data['edit_conflict']=bool(error and error.status==409)
    return page(request,'materials/detail.html',data,error)


def editor_protected(view):
    """Тот же authoritative access protocol; ошибки API никогда не содержат старый ввод."""
    @wraps(view)
    def wrapped(request,*args,**kwargs):
        try:
            return view(request,*args,**kwargs)
        except AccessDenied as exc:
            return JsonResponse({'state':exc.code},status=exc.status)
        except DatabaseError:
            return JsonResponse({'state':'save_unconfirmed' if request.method=='POST' else 'unavailable'},status=503)
    return wrapped


def editor_values(data):
    """Транспорт уже materialized scoped selectors; никаких расчётов/ORM в React."""
    item=data.get('material')
    source_assignments=[item['assignment']] if item else data['assignments']
    assignments=[]
    for row in source_assignments:
        policy=row['policy']
        assignments.append({'id':row['id'],'label':f"{row['course']} · {row['teacher']}",
            'discipline':row['course'],'group':row['group'],'period':row['period'],
            'instructor_person':row['teacher_person'],'available_until':row['available_until'],
            'policy':dict(policy,allow_text='text' in policy['modes'],allow_link='link' in policy['modes'],
                allow_email='email' in policy['modes'],url_limit=1500,email_limit=254) if policy else None})
    material=None
    if item:
        latest=item['revisions'][0] if item['revisions'] else None
        material={'id':item['id'],'assignment_id':item['assignment']['id'],
            'title':item['draft_title'],'mode':item['draft_mode'],'content':item['draft_content'],
            'version':item['draft_version'],'has_draft':item['has_draft'],
            'status':'Черновик' if item['has_draft'] else (latest['review']['status_label'] if latest and latest['review'] else 'Ожидает отзыва'),
            'url':item['url'],'can_submit':item['can_submit'],'revisions':item['revisions'],
            'submitted_count':item['submitted_count'],'updated_at':item['updated_at'],'attachments':item['attachments'],
            'task_id':item['task_id'],'task_revision_id':item['task_revision_id'],'task_url':item['task_url'],'task_title':item['task_title']}
    return {'marker':data['marker'],'assignments':assignments,'material':material}


def editor_response(data, *, state='ready', error=None, created=None):
    payload=editor_values(data)
    payload['state']=state
    if error:
        payload['error']={'message':error.message,'field':error.field}
    if created is not None:
        payload['created']=created
    if payload['material']:
        payload['url']=payload['material']['url']
    response=JsonResponse(payload,status=error.status if error else 200)
    response['X-Page-Context']=data['marker']
    return response


@require_http_methods(['GET','POST'])
@sensitive_post_parameters('title','content','context')
@editor_protected
def editor(request,material_id=None):
    if request.method=='GET':
        expected=request.headers.get('X-Page-Context')
        return editor_response(snapshot(request,material_id=material_id,expected_marker=expected,editor_only=True))
    if request.content_type=='application/json':
        # Даже синтаксическая ошибка проверяется после текущей сессии/роли.
        actor=current_actor(request,expected=request.headers.get('X-Page-Context',''))
        services.role(actor,'student')
        try:
            payload=json.loads(request.body)
            if not isinstance(payload,dict): raise ValueError('object required')
        except (ValueError,UnicodeDecodeError):
            data=snapshot(request,material_id=material_id,expected_marker=actor.marker,editor_only=True)
            return editor_response(data,state='invalid',error=services.MaterialError('Не удалось прочитать форму. Повторите отправку из редактора.'))
    else:
        payload=request.POST
    marker=payload.get('context','')
    if not isinstance(marker,str): marker=''
    action=payload.get('action','')
    error=None
    result=None
    try:
        if 'operation' in payload or 'attachment_ids' in payload or request.FILES or getattr(request,'material_upload_error',False):
            from .attachments import perform
            result=perform(request,marker,payload,material_id)
        elif action=='save':
            result=services.save_draft(request,marker,payload,material_id)
        elif action=='submit' and material_id is not None:
            result=services.submit_material(request,marker,material_id,payload.get('expected_version'),
                payload.get('expected_policy'),payload)
        elif action=='correct' and material_id is not None:
            result=services.start_correction(request,marker,material_id,payload.get('expected_version'))
        else:
            services.role(current_actor(request,expected=marker),'student')
            raise services.MaterialError('Выберите действие в редакторе.')
    except services.MaterialError as exc:
        error=exc
    # Commit либо отказ уже произошёл. Fresh read guard исключает ответ в новом context.
    data=snapshot(request,material_id=result['id'] if result else material_id,expected_marker=marker,editor_only=True)
    state=('conflict' if error.status==409 else 'invalid') if error else {'save':'saved','submit':'submitted','correct':'corrected'}[action]
    return editor_response(data,state=state,error=error,created=result.get('created') if result else None)


@require_http_methods(['GET','POST'])
@sensitive_post_parameters('feedback','context')
@protected
def teacher_detail(request,material_id):
    error=None
    attempted=request.POST.get('revision','')
    if request.method=='POST':
        try:
            # Не позволяем URL одной работы выступать оболочкой записи в другую.
            data=snapshot(request,material_id=material_id,teacher=True)
            if attempted not in {str(row['id']) for row in data['material']['revisions']}:
                raise AccessDenied('not_found',404)
            result=services.review_revision(request,request.POST.get('context',''),int(attempted),request.POST)
            return saved(request,result,f'/teacher/materials/{material_id}/')
        except services.MaterialError as exc: error=exc
    data=snapshot(request,material_id=material_id,teacher=True)
    from workspaces.selectors import review_queue
    actor=current_actor(request,expected=data['marker'])
    queue=review_queue(actor,actor.checked_at,{'state':'pending'})
    data['next_work']=next((r['url'] for r in queue['items'] if not r['url'].startswith(f'/teacher/materials/{material_id}/')),'')
    for revision in data['material']['revisions']:
        if not revision['review']:
            bound=request.POST if error and attempted==str(revision['id']) else None
            revision['form']=form_error(ReviewForm(bound,auto_id=f"review_{revision['id']}_%s"),error if bound is not None else None)
    if error:
        data['unsaved_review']=request.POST.get('feedback','')[:2000]
        data['unsaved_result']=request.POST.get('result','')
    return page(request,'materials/teacher_detail.html',data,error)


@require_http_methods(['GET','POST'])
@sensitive_post_parameters('instructions','context')
@protected
def policy(request,assignment_id):
    error=None
    if request.method=='POST':
        try:
            result=services.update_policy(request,request.POST.get('context',''),assignment_id,request.POST)
            return saved(request,result,f'/teacher/assignments/{assignment_id}/')
        except services.MaterialError as exc: error=exc
    data=snapshot(request,assignment_id=assignment_id,teacher=True)
    old=data['assignment']['policy']
    current_version=old['version'] if old else 0
    if error and str(current_version)!=request.POST.get('expected_version'):
        error=services.MaterialError('Условия изменились. Проверьте сохранённую версию.',status=409)
    initial={key:old[key] for key in ('modes','text_limit','cloud_hosts','instructions')} if old else {'modes':['text'],'text_limit':5000,'cloud_hosts':[]}
    if old:
        initial.update({key:old[key] for key in ('attachments_allowed','attachment_types','attachment_count')})
        initial.update(attachment_file_mib=old['attachment_file_limit']//1024//1024,attachment_total_mib=old['attachment_total_limit']//1024//1024)
    bound=request.POST if error and error.status!=409 else None
    data['form']=form_error(PolicyForm(bound,initial=initial),error if bound is not None else None)
    data['policy_version']=current_version
    data['edit_conflict']=bool(error and error.status==409)
    return page(request,'materials/policy.html',data,error)


@require_GET
@protected
def download(request,attachment_id,revision_id=None):
    from io import BytesIO
    from django.db import transaction
    from django.http import FileResponse
    from accounts.access import lock_guard
    from .models import MaterialAttachment, DraftAttachment, RevisionAttachment
    with transaction.atomic():
        guard=lock_guard();actor=current_actor(request,guard=guard,lock=True)
        if revision_id is None:
            services.role(actor,'student')
            links=DraftAttachment.objects.filter(material__in=selectors.material_scope(actor,actor.checked_at),attachment_id=attachment_id)
        else:
            links=RevisionAttachment.objects.filter(revision__in=selectors.revision_scope(actor,actor.checked_at),revision_id=revision_id,attachment_id=attachment_id)
        link=links.select_related('attachment').first()
        if not link: raise AccessDenied('not_found',404)
        file=link.attachment
        current_actor(request,guard=guard,lock=True,expected=actor.marker)
        response=FileResponse(BytesIO(bytes(file.data)),as_attachment=True,filename=file.name,content_type=file.mime)
        response['X-Content-Type-Options']='nosniff'
        response['Cache-Control']='private, no-store'
        response['Content-Security-Policy']="default-src 'none'; sandbox"
        return response
