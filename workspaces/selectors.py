"""Текущие отношения проверяются до данных, поиска, counts и pagination."""
from datetime import datetime, timedelta
from urllib.parse import urlencode
from django.core.paginator import Paginator
from django.db.models import Q, F, Exists, OuterRef
from django.db.models import DateTimeField
from django.db.models.functions import Cast
from django.db.models.fields.json import KeyTextTransform
from accounts.access import AccessDenied
from accounts.models import Identity, StaffScope
from studies.models import StudentProfile, Enrollment, Lesson, AssessmentEvent, Instructor, ScheduleChange, AssessmentChange
from studies.selectors import moscow, exact_moscow, lesson_values, history_values, schedule_day
from materials.selectors import assignment_scope, material_scope, REVIEW_LABELS
from materials.models import Material, MaterialRevision
from profiles.selectors import person_for_identity
from .models import TeachingPublication, CourseTask, TaskMember, PublicationFile, StaffNotice, Clarification


def participants(assignment):
    enrollment=Enrollment.objects.filter(student_id=OuterRef('pk'),course_id=assignment.course_id,
        period_id=assignment.period_id,status='active',dataset_id=assignment.course.dataset_id)
    return StudentProfile.objects.filter(group_id=assignment.group_id,dataset_id=assignment.course.dataset_id).annotate(
        enrolled=Exists(enrollment)).filter(enrolled=True).order_by('name','pk')


def teaching_scope(actor,now,*,archive=False):
    if actor.role not in ('student','teacher'): raise AccessDenied('forbidden',403)
    query=TeachingPublication.objects.filter(assignment__in=assignment_scope(actor,now))
    if actor.role=='student':
        query=query.filter(state='published',published_revision__isnull=False)
        query=query.filter(Q(kind='resource')|Q(task__members__student_id=actor.profile_id)).distinct()
    return query


def task_scope(actor,now):
    return CourseTask.objects.filter(publication__in=teaching_scope(actor,now),publication__state='published')


def notice_scope(actor,now,*,archive=False):
    if actor.role=='staff':
        faculty=StaffScope.objects.filter(identity=actor.identity).values('faculty_id')
        return StaffNotice.objects.filter(faculty_id__in=faculty).filter(Q(group__isnull=True)|Q(group__programme__faculty_id=F('faculty_id')))
    if actor.role!='student': raise AccessDenied('forbidden',403)
    query=StaffNotice.objects.filter(faculty_id=actor.identity.profile.group.programme.faculty_id,
        faculty__dataset_id=actor.identity.profile.dataset_id,published_revision__isnull=False).filter(Q(group__isnull=True)|Q(group_id=actor.group_id))
    if not archive:
        query=query.annotate(visibility_end=Cast(KeyTextTransform('visible_until','published_revision__payload'),DateTimeField()))
        query=query.filter(state='published').filter(Q(visibility_end__isnull=True)|Q(visibility_end__gt=now))
    return query


def file_values(file, revision=None):
    return {'id':str(file.pk),'name':file.name,'size':file.size,'kind':file.kind,'sha256':file.sha256,
        'url':f'/teaching/files/{file.pk}/'+(f'?revision={revision.pk}' if revision else '')}


def revision_values(revision,now):
    p=revision.payload
    return {'id':revision.pk,'number':revision.number,'payload':p,'state':revision.state,'created_at':moscow(revision.created_at),
        'state_label':{'published':'Опубликовано','withdrawn':'Отозвано','archived':'В архиве'}[revision.state],
        'opens_label':exact_moscow(datetime.fromisoformat(p['opens_at'])) if p.get('opens_at') else '',
        'cutoff_label':exact_moscow(datetime.fromisoformat(p['cutoff_at'])) if p.get('cutoff_at') else '',
        'file_mib':p.get('attachment_file_limit',0)//1048576,'total_mib':p.get('attachment_total_limit',0)//1048576,
        'acceptance_label':('Приём закрыт преподавателем' if not p.get('accepting') else
            'Приём ещё не открыт' if p.get('opens_at') and now<datetime.fromisoformat(p['opens_at']) else
            'Приём завершён: наступило явное окончание' if p.get('cutoff_at') and now>=datetime.fromisoformat(p['cutoff_at']) else 'Приём открыт'),
        'files':[file_values(f,revision) for f in ordered_files(revision.publication_id,revision.files)],
        'due_label':exact_moscow(datetime.fromisoformat(p['due_at'])) if p.get('due_at') else 'Срок не указан'}


def ordered_files(publication_id,ids):
    files={str(f.pk):f for f in PublicationFile.objects.filter(publication_id=publication_id,pk__in=ids).defer('data')}
    return [files[i] for i in ids if i in files]


def publication_values(actor,row,now,*,detail=False):
    rev=row.published_revision
    p=row.draft if actor.role=='teacher' and not rev else rev.payload
    a=row.assignment
    prefix='/teacher/publications/' if actor.role=='teacher' else '/study/teaching/'
    result={'id':row.pk,'kind':row.kind,'kind_label':'Задание' if row.kind=='task' else 'Материал преподавателя',
        'title':p.get('title','Без названия'),'topic':p.get('topic',''),'state':row.state,
        'state_label':{'draft':'Черновик','published':'Опубликовано','withdrawn':'Отозвано','archived':'В архиве'}[row.state],
        'course':a.course.name,'group':a.group.name,'period':a.period.name,'teacher':a.teacher.instructor.name,
        'assignment_id':a.pk,'url':f'{prefix}{row.pk}/','version':row.version,'revision':revision_values(rev,now) if rev else None,
        'due_label':exact_moscow(datetime.fromisoformat(p['due_at'])) if p.get('due_at') else 'Срок не указан'}
    if row.kind=='task':
        task=row.task
        result['task_id']=task.pk
        if actor.role=='student':
            response=Material.objects.filter(task=task,owner_id=actor.profile_id).first()
            result['response_url']=f'/study/materials/{response.pk}/' if response else ''
            result['response_status']=response_state(response)
        if actor.role=='teacher' and detail: result['roster']=task_roster(actor,task,now)
    if detail:
        result['history']=[revision_values(v,now) for v in row.revisions.order_by('-number')]
        if actor.role=='teacher':
            result.update(draft=row.draft,draft_files=[file_values(f) for f in ordered_files(row.pk,row.draft_files)])
    return result


def response_state(row):
    latest=row.revisions.select_related('review').order_by('-number').first() if row else None
    review=getattr(latest,'review',None) if latest else None
    return {'label':REVIEW_LABELS[review.status] if review else 'Ожидает проверки' if latest else 'Не отправлено',
        'code':review.status if review else 'pending' if latest else 'unsubmitted','number':latest.number if latest else None,
        'submitted_at':moscow(latest.submitted_at) if latest else None,'late':latest.task_conditions.get('late',False) if latest else False,
        'deadline_applies':latest.task_conditions.get('deadline_applies',False) if latest else False,
        'result':review_result(review.result) if review else ''}


def review_result(value):
    return {'pass':'Зачтено','fail':'Не зачтено','':'Без отметки'}.get(value,value)


def task_roster(actor,task,now):
    if actor.role!='teacher' or not teaching_scope(actor,now).filter(pk=task.publication_id).exists(): raise AccessDenied('not_found',404)
    rows=[];published=task.publication.published_revision
    due=published.payload.get('due_at') if published else None;due=datetime.fromisoformat(due) if due else None
    for member in task.members.filter(student__in=participants(task.publication.assignment)).select_related('student').order_by('student__name','pk'):
        response=material_scope(actor,now).filter(task=task,owner=member.student).first()
        state=response_state(response)
        if state['code']=='unsubmitted' and due and member.assigned_at<=due<now: state['label']='Срок прошёл, работа не отправлена'
        identity=Identity.objects.select_related('user','profile').filter(profile=member.student).first()
        rows.append({'person':person_for_identity(actor,identity) if identity else {'name':member.student.name,'avatar_url':''},
            'assigned_at':moscow(member.assigned_at),'state':state,'url':f'/teacher/materials/{response.pk}/' if response else ''})
    return rows


def paginate(query,params,path,*,size=10):
    raw=params.get('page','1')
    if not str(raw).isdigit() or len(str(raw))>8 or int(raw)<1: raise AccessDenied('invalid_query',400)
    page=Paginator(query,size).get_page(raw)
    def url(number): return path+'?'+urlencode({**dict(params),'page':number})
    return page,{'page_obj':page,'page_previous_url':url(page.previous_page_number()) if page.has_previous() else '',
        'page_next_url':url(page.next_page_number()) if page.has_next() else '',
        'page_notice':'Такой страницы нет. Показана последняя доступная.' if page.number!=int(raw) else ''}


def review_queue(actor,now,params):
    query=MaterialRevision.objects.filter(material__in=material_scope(actor,now),number=F('material__submission_count'))
    for param,field in [('course','material__assignment__course_id'),('group','material__assignment__group_id'),('task','material__task_id')]:
        value=params.get(param,'')
        if value:
            if not str(value).isdigit(): raise AccessDenied('invalid_query',400)
            query=query.filter(**{field:value})
    state=params.get('state','pending')
    if state=='pending': query=query.filter(review__isnull=True)
    elif state in REVIEW_LABELS: query=query.filter(review__status=state)
    elif state!='all': raise AccessDenied('invalid_query',400)
    query=query.select_related('material__assignment__course','material__assignment__group','material__owner','review').order_by('submitted_at','pk')
    page,pager=paginate(query,params,'/teacher/review/')
    items=[]
    for rev in page:
        state=response_state(rev.material)
        items.append({'title':rev.title,'number':rev.number,'student':rev.material.owner.name,'course':rev.material.assignment.course.name,
            'group':rev.material.assignment.group.name,'state':state,'url':f'/teacher/materials/{rev.material_id}/#revision-{rev.pk}'})
    return {**pager,'items':items,'count':page.paginator.count,'filters':params}


def teacher_schedule(actor,now):
    from data_import.models import Dataset, ImportRevision, Coverage
    if actor.role!='teacher': raise AccessDenied('forbidden',403)
    assignments=assignment_scope(actor,now)
    dataset=actor.identity.instructor.dataset_id
    head=Dataset.objects.get(pk=dataset)
    rev=ImportRevision.objects.filter(dataset=head,revision=head.current_revision).first()
    scope=Q(pk__in=[])
    for a in assignments: scope|=Q(course_id=a.course_id,group_id=a.group_id,period_id=a.period_id)
    lessons=Lesson.objects.filter(scope,dataset_id=dataset).select_related('course','instructor','group')
    assessments=AssessmentEvent.objects.filter(scope,dataset_id=dataset).select_related('course','instructor','group')
    rows=[];history=[]
    instructors=dict(Instructor.objects.filter(dataset_id=dataset).values_list('source_id','name'))
    instructor_source=actor.identity.instructor.source_id
    for objects,model,field,kind in [(lessons,ScheduleChange,'lesson','lesson'),(assessments,AssessmentChange,'assessment','assessment')]:
        for obj in objects:
            own=obj.instructor_id==actor.identity.instructor_id
            changes=model.objects.filter(**{field:obj}).order_by('recorded_at','pk')
            if not own:changes=changes.filter(Q(before__instructor_id=instructor_source)|Q(after__instructor_id=instructor_source))
            changes=list(changes)
            if not own and not changes:continue
            val=lesson_values(obj);val.update(id=f'{kind}:{obj.pk}',url=f'/teacher/schedule/{kind}/{obj.pk}/',group=obj.group.name,
                course_name=obj.course.name+(' · Сессия' if kind=='assessment' else ''),kind=kind)
            if own:rows.append(val)
            else:val.update(participation_removed=True,status='cancelled',status_label='Ваше участие завершено')
            for change in changes:
                if change.before and change.after: history.append(history_values(change,val,instructors))
    rows.sort(key=lambda r:(r['starts_at'],r['source_id'],r['kind']))
    valid=bool(rev and rev.valid_from<=now<rev.valid_until)
    coverage=list(Coverage.objects.filter(revision=rev,group_id__in=assignments.values('group_id')).values('period_id','date_from','date_until','completeness')) if rev else []
    # Source declares group coverage, not a full Instructor availability calendar.
    source={'state':'ready' if valid else 'expired','message':'Показаны события, где вы назначены преподавателем, в пределах ваших дисциплин и групп. Полная занятость источником не подтверждена.',
        'source_as_of':moscow(rev.source_as_of) if rev else None,'imported_at':moscow(rev.imported_at) if rev else None,
        'valid_until':moscow(rev.valid_until) if rev else None,'coverage_from':None,'coverage_until':None,'update_message':''}
    return {'source':source,'lessons':rows,'history':history,'coverage':coverage,'periods':{},'now':now}


def teacher_day(snapshot,date):
    result=schedule_day(snapshot,date)
    result.update(state='partial' if snapshot['source']['state']=='ready' else 'expired',total=None,
        count_label=f"Известных событий: {len(result['lessons'])}",
        message='Показаны известные события ваших назначений; полная занятость не подтверждена.' if snapshot['source']['state']=='ready' else 'Срок действия источника истёк. Показаны сохранённые события.')
    return result


def clarification_values(actor,row):
    from student_requests.selectors import request_scope
    if not request_scope(actor).filter(pk=row.pk).exists(): raise AccessDenied('not_found',404)
    result=[]
    for exchange in Clarification.objects.filter(request=row).order_by('created_at','pk'):
        result.append({'id':exchange.pk,'version':exchange.version,'state':exchange.state,
            'label':{'open':'Ожидаем уточнение','answered':'Ответ получен','resolved':'Уточнение завершено'}[exchange.state],
            'messages':[{'action':m.action,'body':m.body,'at':moscow(m.created_at),'actor':'Студент' if m.action=='reply' else 'Сотрудник'} for m in exchange.messages.order_by('number')]})
    return result


def student_today(actor,now):
    """Application content survives source expiry; authority still uses current relations."""
    tasks=[]
    for row in teaching_scope(actor,now).filter(kind='task').select_related('published_revision','assignment__course','assignment__group','assignment__period','assignment__teacher__instructor'):
        value=publication_values(actor,row,now)
        if value['response_status']['code'] not in ('unsubmitted','changes_requested'):continue
        member=row.task.members.get(student_id=actor.profile_id);p=row.published_revision.payload
        due=datetime.fromisoformat(p['due_at']) if p.get('due_at') else None
        if not p.get('accepting') or p.get('cutoff_at') and datetime.fromisoformat(p['cutoff_at'])<=now:continue
        if p.get('opens_at') and datetime.fromisoformat(p['opens_at'])>now:continue
        overdue=bool(due and member.assigned_at<=due<now)
        tasks.append({'key':f'task:{row.pk}','rank':0 if overdue else 1 if due and due<=now+timedelta(days=7) else 3,
            'sort_at':due or now,'kind':'task','kind_label':'Задание преподавателя','title':value['title'],
            'context':value['response_status']['label'],'detail':value['course'],'due_at':due,'due_exact':value['due_label'],
            'due_label':'Срок задания' if due else 'Срок не указан','url':value['url'],'overdue':overdue})
    notices=[]
    for row in notice_scope(actor,now).select_related('published_revision','faculty').order_by('-published_revision__created_at','-pk'):
        p=row.published_revision.payload;due=datetime.fromisoformat(p['due_at']) if p.get('due_at') else None
        notices.append({'id':f'faculty-{row.pk}','title':p['title'],'body':p['body'],'due_at':due,
            'deadline_elapsed':bool(due and due<now),'due_exact':exact_moscow(due) if due else '',
            'url':f'/notices/{row.pk}/','attribution':'Служба факультета · '+row.faculty.name})
    return tasks,notices
