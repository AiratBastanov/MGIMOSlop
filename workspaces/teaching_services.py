"""Публикация текущего payload, immutable revision, exact files, scoped cohort."""
import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from django.conf import settings
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from accounts.access import current_actor, lock_guard, AccessDenied
from materials.services import MaterialError, text, version, role
from materials.selectors import assignment_scope, CLOUD_HOSTS
from materials.attachments import inspect_upload, validate_set
from materials.uploads import KINDS
from materials.models import SubmissionPolicy
from .models import TeachingPublication, TeachingRevision, PublicationFile, CourseTask, TaskMember
from .common import https_link, instant, operation, record_operation, emit, bounded_int


def payload(data, kind):
    result={k:text(data.get(k,''),field=k,limit=limit,required=k=='title') for k,limit in [('title',160),('topic',500),('body',20000)]}
    raw=data.get('links','')
    if not isinstance(raw,str) or len(raw)>5000: raise MaterialError('Допустимо до пяти HTTPS-ссылок.',field='links')
    links=[https_link(v.strip(),'links') for v in raw.splitlines() if v.strip()]
    if len(links)>5: raise MaterialError('Допустимо до пяти ссылок.',field='links')
    result['links']=links
    if kind=='task':
        for name in ('opens_at','due_at','cutoff_at'): result[name]=instant(data.get(name,''),name)
        times={k:datetime.fromisoformat(result[k]) if result[k] else None for k in ('opens_at','due_at','cutoff_at')}
        if times['opens_at'] and times['due_at'] and times['due_at']<times['opens_at']:
            raise MaterialError('Срок не может быть раньше открытия приёма.',field='due_at')
        if times['cutoff_at'] and any(times[k] and times['cutoff_at']<times[k] for k in ('opens_at','due_at')):
            raise MaterialError('Окончание приёма не может быть раньше открытия или срока.',field='cutoff_at')
        getlist=lambda k,d: data.getlist(k) if hasattr(data,'getlist') else data.get(k,d)
        modes=getlist('modes',['text']);types=getlist('attachment_types',list(KINDS))
        if not modes or not isinstance(modes,list) or set(modes)-{'text','link','email'}:
            raise MaterialError('Выберите допустимые форматы ответа.',field='modes')
        if not isinstance(types,list) or set(types)-set(KINDS):
            raise MaterialError('Проверьте типы файлов.',field='attachment_types')
        mode=data.get('assessment_mode','none')
        if mode not in ('none','numeric','pass_fail'): raise MaterialError('Выберите шкалу результата.',field='assessment_mode')
        result.update(modes=sorted(set(modes)),text_limit=bounded_int(data,'text_limit',5000,100,20000),
            attachment_types=sorted(set(types)),attachment_count=bounded_int(data,'attachment_count',5,1,settings.MATERIAL_ATTACHMENT_COUNT),
            attachment_file_limit=bounded_int(data,'attachment_file_mib',10,1,settings.MATERIAL_ATTACHMENT_FILE_BYTES//1048576)*1048576,
            attachment_total_limit=bounded_int(data,'attachment_total_mib',25,1,settings.MATERIAL_ATTACHMENT_TOTAL_BYTES//1048576)*1048576,
            assessment_mode=mode,accepting=True)
    return result


def publication_policy():
    return SimpleNamespace(attachments_allowed=True,attachment_types=list(KINDS),attachment_count=5,
        attachment_file_limit=10*1048576,attachment_total_limit=25*1048576)


def perform(request, marker, data, publication_id=None):
    from .selectors import teaching_scope, participants
    actor=current_actor(request,expected=marker);role(actor,'teacher')
    if getattr(request,'material_upload_error',False): raise MaterialError('Превышены лимиты загрузки.',field='files')
    uploaded=[inspect_upload(f) for f in request.FILES.getlist('files')]
    try:
        retained=json.loads(data.get('attachment_ids','[]'))
        if not isinstance(retained,list) or len(retained)>5 or len(set(retained))!=len(retained): raise ValueError()
        retained=[str(uuid.UUID(v)) for v in retained]
    except (ValueError,TypeError): raise MaterialError('Набор файлов изменился. Откройте актуальную версию.',status=409)
    with transaction.atomic():
        guard=lock_guard();actor=current_actor(request,guard=guard,lock=True,expected=marker);role(actor,'teacher')
        row=teaching_scope(actor,actor.checked_at).select_for_update(of=('self',)).filter(pk=publication_id).first() if publication_id else None
        if publication_id and not row: raise AccessDenied('not_found',404)
        actor=current_actor(request,guard=guard,lock=True,expected=marker)
        if row and not teaching_scope(actor,actor.checked_at).filter(pk=row.pk).exists(): raise AccessDenied('not_found',404)
        key,digest,retry=operation(actor,data,target=['teaching',publication_id],files=uploaded)
        if retry:
            if not teaching_scope(actor,actor.checked_at).filter(pk=retry['id']).exists(): raise AccessDenied('not_found',404)
            return {**retry,'marker':actor.marker,'repeated':True}
        action=data.get('action')
        if action not in ('save','publish','withdraw','archive','close','reopen','add_members'):
            raise MaterialError('Выберите действие публикации.')
        if not row:
            if action not in ('save','publish'): raise MaterialError('Сначала создайте публикацию.')
            assignment=assignment_scope(actor,actor.checked_at).select_for_update(of=('self',)).filter(pk=version(data.get('assignment'))).first()
            if not assignment: raise AccessDenied('not_found',404)
            kind=data.get('kind')
            if kind not in ('resource','task'): raise MaterialError('Выберите материал или задание.')
            if retained: raise AccessDenied('not_found',404)
            row=TeachingPublication.objects.create(assignment=assignment,kind=kind)
            if kind=='task': CourseTask.objects.create(publication=row)
        elif row.version!=version(data.get('expected_version')):
            raise MaterialError('Публикация уже изменилась. Ввод сохранён в этом окне; откройте актуальную версию отдельно.',status=409)
        actor=current_actor(request,guard=guard,lock=True,expected=marker)
        if not teaching_scope(actor,actor.checked_at).filter(pk=row.pk).exists(): raise AccessDenied('not_found',404)
        if data.get('assignment') and str(row.assignment_id)!=str(data['assignment']):
            raise MaterialError('Аудитория закреплена. Создайте отдельную публикацию для другого назначения.',status=409)
        if action=='add_members':
            if row.kind!='task' or row.state!='published': raise MaterialError('Состав доступен только у опубликованного задания.',status=409)
            added=0
            for student in participants(row.assignment):
                _,created=TaskMember.objects.get_or_create(task=row.task,student=student,defaults={'assigned_at':timezone.now(),'assigned_revision':row.published_revision})
                added+=created
            # События прежней версии уже имеют receipt только прежних eligible получателей.
            from notifications.models import Notification
            from notifications.delivery import distribute
            distribute(Notification.objects.get(application_event__teaching_revision=row.published_revision))
            row.version+=1;row.save(update_fields=['version','updated_at'])
            return record_operation(actor,key,digest,{'id':row.pk,'added':added,'state':row.state})
        if action in ('save','publish'):
            content=payload(data,row.kind)
            if row.kind=='task' and row.published_revision_id:
                content['accepting']=row.published_revision.payload['accepting']
            old={str(f.pk):f for f in PublicationFile.objects.filter(publication=row,pk__in=retained).defer('data')}
            if set(old)!=set(retained): raise AccessDenied('not_found',404)
            if not set(retained)<=set(row.draft_files):
                raise MaterialError('Состав файлов изменился. Сравните сохранённую версию и выберите файлы заново.',status=409)
            validate_set(publication_policy(),[{'kind':f.kind,'size':f.size} for f in old.values()]+uploaded)
            selected=retained+[str(PublicationFile.objects.create(publication=row,**f).pk) for f in uploaded]
            if row.kind=='task' and row.published_revision_id:
                previous_mode=row.published_revision.payload['assessment_mode']
                from materials.models import MaterialReview
                if content['assessment_mode']!=previous_mode and MaterialReview.objects.filter(revision__material__task=row.task).exists():
                    raise MaterialError('Шкала уже использована в отзывах и не может быть изменена.',field='assessment_mode',status=409)
            row.draft=content;row.draft_files=selected
        else:
            if not row.published_revision_id: raise MaterialError('У публикации пока нет опубликованной версии.',status=409)
            content=dict(row.published_revision.payload);selected=list(row.published_revision.files)
            if action in ('close','reopen'):
                if row.kind!='task' or row.state!='published': raise MaterialError('Приём меняется у опубликованного задания.',status=409)
                content['accepting']=action=='reopen'
        if action!='save':
            if action=='publish' and not (content['body'] or content['links'] or selected):
                raise MaterialError('Добавьте текст, ссылку или файл перед публикацией.',field='body')
            state='withdrawn' if action=='withdraw' else 'archived' if action=='archive' else 'published'
            number=(row.revisions.aggregate(n=Max('number'))['n'] or 0)+1
            rev=TeachingRevision.objects.create(publication=row,number=number,payload=content,files=selected,state=state,author=actor.identity)
            if row.kind=='task':
                # Общая immutable policy совместима с существующими DB triggers ответа.
                n=(SubmissionPolicy.objects.filter(assignment=row.assignment).aggregate(n=Max('version'))['n'] or 0)+1
                SubmissionPolicy.objects.create(assignment=row.assignment,version=n,task_revision=rev,author_id=actor.user_id,
                    modes=content['modes'],text_limit=content['text_limit'],cloud_hosts=list(CLOUD_HOSTS),instructions=content['topic'],
                    attachments_allowed=bool(content['attachment_types']),attachment_types=content['attachment_types'],
                    attachment_count=content['attachment_count'],attachment_file_limit=content['attachment_file_limit'],attachment_total_limit=content['attachment_total_limit'])
                if not row.published_revision_id:
                    now=timezone.now()
                    TaskMember.objects.bulk_create([TaskMember(task=row.task,student=s,assigned_at=now,assigned_revision=rev) for s in participants(row.assignment)])
            row.published_revision=rev;row.state=state
        row.version+=1;row.save()
        if action!='save': emit('teaching',teaching_revision=rev)
        return record_operation(actor,key,digest,{'id':row.pk,'state':row.state})


def task_submission(actor, material, expected_revision, *, now):
    """После idempotency receipt: проверка текущей публикации и её time policy."""
    from .selectors import task_scope
    task=task_scope(actor,now).select_related('publication__published_revision').filter(pk=material.task_id).first()
    if not task: raise AccessDenied('not_found',404)
    rev=task.publication.published_revision
    if str(rev.pk)!=str(expected_revision):
        raise MaterialError('Условия задания изменились. Текущий текст и файлы не отправлены; проверьте новую версию.',status=409)
    policy=rev.payload
    opens=datetime.fromisoformat(policy['opens_at']) if policy.get('opens_at') else None
    cutoff=datetime.fromisoformat(policy['cutoff_at']) if policy.get('cutoff_at') else None
    due=datetime.fromisoformat(policy['due_at']) if policy.get('due_at') else None
    if not policy['accepting']: raise MaterialError('Преподаватель закрыл приём. Черновик остаётся у вас.',status=409)
    if opens and now<opens: raise MaterialError('Приём ещё не открыт. Черновик можно сохранить.',status=409)
    if cutoff and now>=cutoff: raise MaterialError('Явный срок окончания приёма наступил. Новая отправка не принята.',status=409)
    member=TaskMember.objects.get(task=task,student_id=actor.profile_id)
    return rev,{'opens_at':policy.get('opens_at'),'due_at':policy.get('due_at'),'cutoff_at':policy.get('cutoff_at'),
        'accepted_at':now.isoformat(),'late':bool(due and now>due),'deadline_applies':bool(due and member.assigned_at<=due),
        'assigned_at':member.assigned_at.isoformat(),'assessment_mode':policy['assessment_mode']}
