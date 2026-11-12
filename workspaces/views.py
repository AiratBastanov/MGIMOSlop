import calendar
from datetime import timedelta,datetime
from functools import wraps
from io import BytesIO
from django.db import transaction
from django.db.models import Q
from django.http import JsonResponse, FileResponse
from django.shortcuts import render,redirect
from django.views.decorators.http import require_GET,require_POST,require_http_methods
from django.views.decorators.debug import sensitive_post_parameters
from django.utils import timezone
from accounts.access import AccessDenied,current_actor,lock_guard
from accounts.models import StaffScope,Identity
from accounts.selectors import private_snapshot
from accounts.views import protected,validate_schedule_params
from accounts.components import component_response,schedule_data,enhanced
from studies.selectors import moscow
from materials.services import MaterialError,role
from materials.selectors import assignment_scope,assignment_values,material_scope
from profiles.selectors import publisher_groups
from profiles.models import GroupResource,GroupResourceRevision
from . import selectors as s,editors
from .models import TeachingPublication,PublicationFile,TeachingRevision,StaffNotice,Clarification


def snapshot(request,expected_role,extension):
    def extend(actor,now):
        role(actor,expected_role)
        return extension(actor,now)
    data=private_snapshot(request,extension=extend)
    if request.method=='POST' or enhanced(request): current_actor(request,expected=request.headers.get('X-Page-Context') or request.POST.get('context',''))
    return data


def page(request,template,data):
    response=render(request,template,data);response['X-Page-Context']=data['marker'];return response


def event_view(actor,now,**filters):
    from accounts.selectors import domain_notifications
    from notifications.inbox_actions import snapshot_token
    from notifications.models import NotificationReceipt
    receipts=NotificationReceipt.objects.filter(user_id=actor.user_id,available_at__lte=now).values('notification_id')
    ids=list(domain_notifications(actor,now=now,archive=True).filter(pk__in=receipts,**filters).values_list('pk',flat=True))
    return {'marker':actor.marker,'token':snapshot_token(actor,ids,'viewed-object')} if ids else None


@require_GET
@protected
def teacher_home(request):
    def extend(actor,now):
        schedule=s.teacher_schedule(actor,now)
        upcoming=next((r for r in schedule['lessons'] if r['status']=='active' and r['ends_at']>now),None) if schedule['source']['state']=='ready' else None
        queue=s.review_queue(actor,now,{'state':'pending'})
        return {'section':'teacher_home','heading':'Обзор преподавателя','next_event':upcoming,'schedule_message':schedule['source']['message'],
            'queue':queue['items'][:3],'queue_count':queue['count'],'assignments':list(assignment_scope(actor,now).select_related('course','group','period'))}
    return page(request,'workspaces/teacher_home.html',snapshot(request,'teacher',extend))


@require_GET
@protected
def courses(request,assignment_id=None):
    def extend(actor,now):
        rows=assignment_scope(actor,now).select_related('course','group','period','teacher__instructor').order_by('course__name','period__name','group__name','pk')
        if not assignment_id:
            groups={}
            for a in rows:
                key=(a.course_id,a.period_id);groups.setdefault(key,{'course':a.course.name,'period':a.period.name,'allocations':[]})['allocations'].append({'id':a.pk,'group':a.group.name})
            return {'section':'teacher_courses','courses':list(groups.values())}
        a=rows.filter(pk=assignment_id).first()
        if not a:raise AccessDenied('not_found',404)
        tab=request.GET.get('tab','resources')
        if tab not in ('resources','tasks','results','participants'):raise AccessDenied('invalid_query',400)
        publications=s.teaching_scope(actor,now).filter(assignment=a).select_related('published_revision','assignment__course','assignment__group','assignment__period','assignment__teacher__instructor').order_by('-updated_at','-pk')
        if tab in ('resources','tasks'):publications=publications.filter(kind='task' if tab=='tasks' else 'resource')
        persons=[]
        if tab=='participants':
            from profiles.selectors import person_for_identity
            for p in s.participants(a):
                identity=Identity.objects.select_related('profile','user').filter(profile=p).first()
                persons.append(person_for_identity(actor,identity) if identity else {'name':p.name,'avatar_url':''})
        publication_page,pager=s.paginate(publications,request.GET.dict(),f'/teacher/courses/{a.pk}/')
        return {**pager,'section':'teacher_courses','assignment':assignment_values(a,actor),'tab':tab,'publications':[s.publication_values(actor,p,now) for p in publication_page],
            'participants':persons,'queue':s.review_queue(actor,now,{'course':str(a.course_id),'group':str(a.group_id),'state':'all'}) if tab=='results' else None}
    return page(request,'workspaces/course.html' if assignment_id else 'workspaces/courses.html',snapshot(request,'teacher',extend))


@require_GET
@protected
def teacher_schedule(request,kind=None,event_id=None):
    selected,mode=validate_schedule_params(dict(request.GET.lists())) if not kind else (None,'day')
    def extend(actor,now):
        schedule=s.teacher_schedule(actor,now)
        if kind:
            item=next((r for r in schedule['lessons']+[h['current'] for h in schedule['history']] if r['id']==f'{kind}:{event_id}'),None)
            if not item:raise AccessDenied('not_found',404)
            return {'section':'teacher_schedule','lesson':item,'history':[r for r in schedule['history'] if r['current']['id']==item['id']],
                'source':schedule['source'],'day_url':f'/teacher/schedule/?date={item["date"].isoformat()}&view=day'}
        date=selected or moscow(now).date();length=7 if mode=='week' else 1
        start=date-timedelta(days=date.weekday()) if mode=='week' else date
        data={'section':'teacher_schedule','source':schedule['source'],'selected_date':date,'selected_iso':date.isoformat(),'view':mode,
            'range_start':start,'range_end':start+timedelta(days=length-1),'group':'Преподаватель · '+actor.identity.instructor.name,
            'days':[s.teacher_day(schedule,start+timedelta(days=i)) for i in range(length)],
            'month_days':[s.teacher_day(schedule,date.replace(day=n)) for n in range(1,calendar.monthrange(date.year,date.month)[1]+1)],
            'previous_url':f'/teacher/schedule/?date={(date-timedelta(days=length)).isoformat()}&view={mode}',
            'next_url':f'/teacher/schedule/?date={(date+timedelta(days=length)).isoformat()}&view={mode}'}
        return data
    data=snapshot(request,'teacher',extend)
    if kind:return page(request,'workspaces/teacher_event.html',data)
    data['component_data']={**schedule_data(request,data),'base':'/teacher/schedule/','heading':'Расписание преподавателя'}
    if enhanced(request):return component_response(request,data['component_data'])
    return page(request,'schedule.html',data)


@require_GET
@protected
def review_queue(request):
    def extend(actor,now):
        a=assignment_scope(actor,now).select_related('course','group')
        return {'section':'teacher_review',**s.review_queue(actor,now,request.GET.dict()),
            'course_options':list({x.course_id:x.course.name for x in a}.items()),'group_options':list({x.group_id:x.group.name for x in a}.items()),
            'task_options':[(p.task.pk,p.published_revision.payload['title']) for p in s.teaching_scope(actor,now).filter(kind='task',published_revision__isnull=False)]}
    return page(request,'workspaces/review_queue.html',snapshot(request,'teacher',extend))


@require_GET
@protected
def publication(request,publication_id=None):
    def extend(actor,now):
        row=s.teaching_scope(actor,now).select_related('assignment__course','assignment__group','assignment__period','assignment__teacher__instructor','published_revision').filter(pk=publication_id).first() if publication_id else None
        if publication_id and not row:raise AccessDenied('not_found',404)
        kind=request.GET.get('kind','resource')
        if kind not in ('resource','task'):raise AccessDenied('invalid_query',400)
        return {'section':'teacher_courses','item':s.publication_values(actor,row,now,detail=True) if row else None,
            'editor_data':editors.teaching_editor(actor,now,row,kind,request.GET.get('assignment'))}
    return page(request,'workspaces/publication_editor.html',snapshot(request,'teacher',extend))


@require_http_methods(['GET','POST'])
@sensitive_post_parameters('body','context')
@protected
def publication_editor(request,publication_id=None):
    from .teaching_services import perform
    error=None;result=None
    if request.method=='POST':
        try:result=perform(request,request.POST.get('context',''),request.POST,publication_id)
        except MaterialError as exc:error=exc
    def extend(actor,now):
        target=result['id'] if result else publication_id
        row=s.teaching_scope(actor,now).filter(pk=target).first() if target else None
        if target and not row:raise AccessDenied('not_found',404)
        return {'editor_data':editors.teaching_editor(actor,now,row,request.POST.get('kind','resource'))}
    data=snapshot(request,'teacher',extend)['editor_data']
    data.update(state='conflict' if error and error.status==409 else 'invalid' if error else 'saved',error=error.message if error else '',error_field=error.field if error else '')
    return JsonResponse(data,status=error.status if error else 200)


@require_GET
@protected
def student_teaching(request,publication_id=None):
    def extend(actor,now):
        query=s.teaching_scope(actor,now).select_related('assignment__course','assignment__group','assignment__period','assignment__teacher__instructor','published_revision')
        if publication_id:
            row=query.filter(pk=publication_id).first()
            if not row:raise AccessDenied('not_found',404)
            return {'section':'materials','material_tab':'tasks' if row.kind=='task' else 'resources','item':s.publication_values(actor,row,now,detail=True),
                'viewed_events':event_view(actor,now,application_event__teaching_revision=row.published_revision)}
        kind=request.GET.get('kind','resource')
        if kind not in ('resource','task'):raise AccessDenied('invalid_query',400)
        paged,pager=s.paginate(query.filter(kind=kind).order_by('-published_revision__created_at','-pk'),request.GET.dict(),'/study/teaching/')
        return {'section':'materials','material_tab':'tasks' if kind=='task' else 'resources',**pager,
            'publications':[s.publication_values(actor,p,now) for p in paged]}
    return page(request,'workspaces/student_publication.html' if publication_id else 'workspaces/student_teaching.html',snapshot(request,'student',extend))


@require_POST
@protected
def start_response(request,publication_id):
    from materials.models import Material
    from materials.services import save_draft
    with transaction.atomic():
        guard=lock_guard();actor=current_actor(request,guard=guard,lock=True,expected=request.POST.get('context',''));role(actor,'student')
        task=s.task_scope(actor,actor.checked_at).select_for_update(of=('self',)).filter(publication_id=publication_id).first()
        if not task:raise AccessDenied('not_found',404)
        actor=current_actor(request,guard=guard,lock=True,expected=actor.marker)
        if not s.task_scope(actor,actor.checked_at).filter(pk=task.pk).exists():raise AccessDenied('not_found',404)
        row=Material.objects.filter(owner_id=actor.profile_id,task=task).first()
        if not row:
            p=task.publication.published_revision.payload
            result=save_draft(request,actor.marker,{'task':str(task.pk),'assignment':str(task.publication.assignment_id),'title':p['title'],'mode':p['modes'][0],'content':''})
            pk=result['id']
        else:pk=row.pk
    return redirect(f'/study/materials/{pk}/')


@require_GET
@protected
def publication_file(request,file_id):
    with transaction.atomic():
        guard=lock_guard();actor=current_actor(request,guard=guard,lock=True)
        f=PublicationFile.objects.select_related('publication').filter(pk=file_id,publication__in=s.teaching_scope(actor,actor.checked_at)).first()
        if not f:raise AccessDenied('not_found',404)
        revision=request.GET.get('revision')
        if revision:
            if not str(revision).isdigit():raise AccessDenied('not_found',404)
            rev=TeachingRevision.objects.filter(pk=revision,publication=f.publication).first()
            if not rev or str(f.pk) not in rev.files or actor.role=='student' and rev.state!='published':raise AccessDenied('not_found',404)
        elif actor.role!='teacher' or str(f.pk) not in f.publication.draft_files:raise AccessDenied('not_found',404)
        current_actor(request,guard=guard,lock=True,expected=actor.marker)
        response=FileResponse(BytesIO(bytes(f.data)),as_attachment=True,filename=f.name,content_type=f.mime)
        response['X-Content-Type-Options']='nosniff';response['Cache-Control']='private, no-store';response['Content-Security-Policy']="default-src 'none'; sandbox"
        return response


@require_GET
@protected
def staff_home(request):
    from student_requests.selectors import request_scope
    def extend(actor,now):
        rows=request_scope(actor)
        return {'section':'staff_home','pending_count':rows.filter(status__in=['submitted','in_progress']).count(),
            'reply_count':Clarification.objects.filter(request__in=rows,state='answered').count(),
            'faculty':StaffScope.objects.select_related('faculty').get(identity=actor.identity).faculty.name}
    return page(request,'workspaces/staff_home.html',snapshot(request,'staff',extend))


@require_GET
@protected
def staff_groups(request,group_id=None):
    from studies.models import StudentProfile
    def extend(actor,now):
        groups=publisher_groups(actor)
        if group_id:
            group=groups.filter(pk=group_id).first()
            if not group:raise AccessDenied('not_found',404)
            rows,pager=s.paginate(StudentProfile.objects.filter(group=group).order_by('name','pk'),request.GET.dict(),f'/staff/groups/{group.pk}/')
            return {'section':'staff_groups','group_item':group,'students':[{'name':r.name,'year':r.year,'source_id':r.source_id} for r in rows],**pager}
        return {'section':'staff_groups','groups':list(groups)}
    return page(request,'workspaces/staff_groups.html',snapshot(request,'staff',extend))


@require_GET
@protected
def notices(request,notice_id=None,new=False):
    def extend(actor,now):
        query=s.notice_scope(actor,now).select_related('group','faculty','published_revision').order_by('-updated_at','-pk')
        row=query.filter(pk=notice_id).first() if notice_id else None
        if notice_id and not row:raise AccessDenied('not_found',404)
        if row or new:return {'section':'staff_notices','notice':row,'editor_data':editors.notice_editor(actor,row),'notice_history':list(row.revisions.order_by('-number')) if row else []}
        rows,pager=s.paginate(query,request.GET.dict(),'/staff/notices/')
        return {'section':'staff_notices','notices':list(rows),**pager}
    return page(request,'workspaces/notice_editor.html' if new or notice_id else 'workspaces/notices.html',snapshot(request,'staff',extend))


@require_POST
@protected
def notice_editor(request,notice_id=None):
    from .faculty_services import save_notice
    error=None;result=None
    try:result=save_notice(request,request.POST.get('context',''),request.POST,notice_id)
    except MaterialError as exc:error=exc
    def extend(actor,now):
        pk=result['id'] if result else notice_id
        row=s.notice_scope(actor,now).filter(pk=pk).first() if pk else None
        if pk and not row:raise AccessDenied('not_found',404)
        return {'editor_data':editors.notice_editor(actor,row)}
    data=snapshot(request,'staff',extend)['editor_data'];data.update(state='conflict' if error and error.status==409 else 'invalid' if error else 'saved',error=error.message if error else '')
    return JsonResponse(data,status=error.status if error else 200)


@require_GET
@protected
def student_notice(request,notice_id):
    def extend(actor,now):
        row=s.notice_scope(actor,now,archive=True).select_related('published_revision','faculty','group').filter(pk=notice_id).first()
        if not row:raise AccessDenied('not_found',404)
        p=row.published_revision.payload
        return {'section':'announcement','notice':row,'content':p,'withdrawn':row.state=='withdrawn',
            'expired':bool(p.get('visible_until') and datetime.fromisoformat(p['visible_until'])<=now),
            'viewed_events':event_view(actor,now,application_event__notice_revision=row.published_revision)}
    return page(request,'workspaces/student_notice.html',snapshot(request,'student',extend))


@require_POST
@protected
def clarification_action(request,request_id):
    from .faculty_services import clarify
    error=None;result=None
    try:result=clarify(request,request.POST.get('context',''),request_id,request.POST)
    except MaterialError as exc:
        error=exc
    def extend(actor,now):
        from student_requests.selectors import request_scope
        row=request_scope(actor).filter(pk=request_id).first()
        if not row:raise AccessDenied('not_found',404)
        return {'exchange':{'marker':actor.marker,'request_id':row.pk,'request_version':row.version,
            'role':actor.role,'terminal':row.status in ('ready','rejected'),'items':s.clarification_values(actor,row)}}
    fresh=private_snapshot(request,extension=extend)
    current_actor(request,expected=request.POST.get('context',''))
    return JsonResponse({**(result or {}),'marker':fresh['marker'],'state':'conflict' if error and error.status==409 else 'invalid' if error else 'saved',
        'error':error.message if error else '', 'exchange':fresh['exchange']},status=error.status if error else 200)


@require_POST
@protected
def viewed_object(request):
    from notifications.recipient_actions import decode,record_reads
    from notifications.inbox_actions import InboxConflict
    from notifications.selectors import inbox_unread
    with transaction.atomic():
        actor=current_actor(request,guard=lock_guard(),lock=True,expected=request.POST.get('context',''))
        try:payload=decode(actor,request.POST.get('snapshot',''),'viewed-object')
        except InboxConflict as exc:return JsonResponse({'marker':actor.marker,'state':'conflict','error':str(exc)},status=409)
        record_reads(actor,payload['ids'])
        return JsonResponse({'marker':actor.marker,'state':'saved','unread':inbox_unread(actor,actor.checked_at)})


@require_GET
@protected
def resources(request,resource_id=None,new=False):
    def extend(actor,now):
        query=GroupResource.objects.filter(group__in=publisher_groups(actor)).select_related('group').order_by('-updated_at','-pk')
        row=query.filter(pk=resource_id).first() if resource_id else None
        if resource_id and not row:raise AccessDenied('not_found',404)
        if row or new:return {'section':'resources','resource':row,'editor_data':editors.resource_editor(actor,row),
            'resource_history':list(GroupResourceRevision.objects.filter(resource=row).order_by('-version')) if row else []}
        group=request.GET.get('group','')
        if group:
            if not str(group).isdigit():raise AccessDenied('invalid_query',400)
            query=query.filter(group_id=group)
        rows,pager=s.paginate(query,request.GET.dict(),'/staff/resources/')
        return {'section':'resources','resources':list(rows),'groups':list(publisher_groups(actor)),'selected_group':group,**pager}
    return page(request,'workspaces/resource_editor.html' if new or resource_id else 'workspaces/resources.html',snapshot(request,'staff',extend))


@require_POST
@protected
def resource_editor(request,resource_id=None):
    from .common import operation,record_operation,emit
    from profiles.forms import ResourceForm
    from profiles.services import save_resource
    error=None;result=None
    try:
        with transaction.atomic():
            guard=lock_guard();actor=current_actor(request,guard=guard,lock=True,expected=request.POST.get('context',''));role(actor,'staff')
            row=GroupResource.objects.select_for_update().filter(pk=resource_id,group__in=publisher_groups(actor)).first() if resource_id else None
            if resource_id and not row:raise AccessDenied('not_found',404)
            actor=current_actor(request,guard=guard,lock=True,expected=actor.marker)
            if row and not publisher_groups(actor).filter(pk=row.group_id).exists():raise AccessDenied('not_found',404)
            key,digest,retry=operation(actor,request.POST,target=['resource',resource_id])
            if retry:
                if not GroupResource.objects.filter(pk=retry['id'],group__in=publisher_groups(actor)).exists():raise AccessDenied('not_found',404)
                result=retry
            else:
                expected=str(row.version if row else 0)
                if request.POST.get('expected_version')!=expected:raise MaterialError('Ресурс уже изменился. Текущий ввод не сохранён; сравните версии.',status=409)
                action=request.POST.get('action')
                if action=='save':
                    data=request.POST.copy();data['version']=expected
                    form=ResourceForm(data,instance=row,groups=publisher_groups(actor))
                    if not form.is_valid():raise MaterialError(' '.join(f'{form.fields[k].label if k in form.fields else "Форма"}: {v}' for k,vs in form.errors.items() for v in vs))
                    pk=save_resource(request,actor.marker,form.cleaned_data,resource_id)
                elif row and action in ('archive','restore'):
                    row.archived=action=='archive';row.version+=1;row.save(update_fields=['archived','version','updated_at'])
                    rev=GroupResourceRevision.objects.create(resource=row,version=row.version,actor=actor.identity,
                        payload={k:getattr(row,k).isoformat() if k.startswith('available_') else getattr(row,k) for k in ('group_id','category','title','description','url','available_from','available_until','archived')})
                    emit('resource',resource_revision=rev);pk=row.pk
                else:raise MaterialError('Выберите действие ресурса.')
                result=record_operation(actor,key,digest,{'id':pk})
    except MaterialError as exc:error=exc
    def extend(actor,now):
        pk=result['id'] if result else resource_id
        row=GroupResource.objects.filter(pk=pk,group__in=publisher_groups(actor)).first() if pk else None
        if pk and not row:raise AccessDenied('not_found',404)
        return {'editor_data':editors.resource_editor(actor,row)}
    data=snapshot(request,'staff',extend)['editor_data'];data.update(state='conflict' if error and error.status==409 else 'invalid' if error else 'saved',error=error.message if error else '')
    return JsonResponse(data,status=error.status if error else 200)
