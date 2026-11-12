"""Материалы: guard, fresh session, scoped target, immutable submitted versions."""
from datetime import datetime
import re
import unicodedata
from urllib.parse import urlsplit
from django.core.exceptions import ValidationError
from django.core.validators import validate_email, URLValidator
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from accounts.access import AccessDenied, current_actor, identity_for, lock_guard
from accounts.models import Identity, AccessAudit, SessionContext
from .models import TeachingAssignment, SubmissionPolicy, Material, MaterialRevision, MaterialReview
from .selectors import assignment_scope, material_scope, revision_scope, current_policy, CLOUD_HOSTS, MODE_LABELS, REVIEW_LABELS


class MaterialError(Exception):
    def __init__(self, message, *, field=None, status=400):
        self.message, self.field, self.status = message, field, status
        super().__init__(message)


def text(value, *, field, limit, required=False):
    if not isinstance(value,str):
        raise MaterialError('Введите обычный текст.',field=field)
    value = unicodedata.normalize('NFC',value.replace('\r\n','\n').replace('\r','\n')).strip()
    if required and not value:
        raise MaterialError('Заполните это поле.',field=field)
    if len(value)>limit:
        raise MaterialError(f'Допустимо не более {limit} символов.',field=field)
    if any(unicodedata.category(char) in ('Cc','Cs') and char not in '\n\t' for char in value):
        raise MaterialError('Удалите недопустимые управляющие символы.',field=field)
    return value


def version(value):
    if not re.fullmatch(r'[1-9][0-9]{0,9}',str(value)):
        raise MaterialError('Откройте актуальную версию страницы.',status=409)
    return int(value)


def role(actor, expected):
    if actor.role != expected:
        raise AccessDenied('forbidden',403)


def _actor(request, marker, guard, expected_role):
    actor = current_actor(request,guard=guard,lock=True,expected=marker)
    role(actor,expected_role)
    return actor


def _task_access(actor, row):
    if row.task_id:
        from workspaces.selectors import task_scope
        if not task_scope(actor,actor.checked_at).filter(pk=row.task_id).exists():
            raise AccessDenied('not_found',404)


def _draft(data):
    title = text(data.get('title',''),field='title',limit=160,required=True)
    mode = data.get('mode','')
    if not isinstance(mode,str) or mode not in MODE_LABELS:
        raise MaterialError('Выберите формат материала.',field='mode')
    # Черновик можно сохранить до заполнения ссылки/почты; формат проверяется при отправке.
    content = text(data.get('content',''),field='content',limit=20000)
    return title,mode,content


def validate_submission(mode, content, policy):
    if mode not in policy.modes:
        raise MaterialError('Этот формат больше не разрешён. Выберите формат из текущих условий.',field='mode')
    content = text(content,field='content',limit=policy.text_limit if mode=='text' else 1500,required=True)
    if mode == 'email':
        try:
            validate_email(content)
        except ValidationError:
            raise MaterialError('Введите корректный адрес почты.',field='content')
        if len(content)>254:
            raise MaterialError('Адрес почты слишком длинный.',field='content')
    elif mode == 'link':
        try:
            URLValidator(schemes=['https'])(content)
            parsed = urlsplit(content)
            if (parsed.username or parsed.password or parsed.port not in (None,443)
                    or parsed.hostname not in policy.cloud_hosts or parsed.hostname not in CLOUD_HOSTS):
                raise ValueError('host')
        except (ValidationError,ValueError):
            raise MaterialError('Нужна HTTPS-ссылка на облачный сервис из условий преподавателя.',field='content')
    return content


def save_draft(request, marker, data, material_id=None):
    with transaction.atomic():
        guard=lock_guard(); actor=_actor(request,marker,guard,'student')
        if material_id is None:
            raw_assignment=data.get('assignment','')
            if not re.fullmatch(r'[1-9][0-9]{0,9}',str(raw_assignment)):
                raise MaterialError('Выберите доступного преподавателя и дисциплину.',field='assignment')
            assignment=assignment_scope(actor,actor.checked_at).select_for_update(of=('self',)).filter(pk=raw_assignment).first()
            if not assignment:
                raise MaterialError('Выберите доступного преподавателя и дисциплину.',field='assignment')
            actor=_actor(request,marker,guard,'student')
            if not assignment_scope(actor,actor.checked_at).filter(pk=assignment.pk).exists():
                raise AccessDenied('not_found',404)
            title,mode,content=_draft(data)
            task=None
            if data.get('task'):
                from workspaces.selectors import task_scope
                task=task_scope(actor,actor.checked_at).filter(pk=version(data['task']),publication__assignment=assignment).first()
                if not task: raise AccessDenied('not_found',404)
                if Material.objects.filter(owner_id=actor.profile_id,task=task).exists():
                    raise MaterialError('Ответ на это задание уже существует. Откройте его в разделе «Мои работы».',status=409)
            row=Material.objects.create(owner_id=actor.profile_id,assignment=assignment,task=task,
                draft_title=title,draft_mode=mode,draft_content=content)
        else:
            row=material_scope(actor,actor.checked_at).select_for_update().filter(pk=material_id).first()
            if not row: raise AccessDenied('not_found',404)
            actor=_actor(request,marker,guard,'student')
            _task_access(actor,row)
            expected=version(data.get('expected_version'))
            title,mode,content=_draft(data)
            # Повтор того же сохранения после потерянного ответа не создаёт новую версию.
            if (row.has_draft and row.draft_version == expected+1
                    and (row.draft_title,row.draft_mode,row.draft_content)==(title,mode,content)):
                return {'id':row.pk,'marker':actor.marker,'created':False}
            if row.draft_version != expected or not row.has_draft:
                raise MaterialError('Материал уже изменился. Откройте сохранённую версию перед редактированием.',status=409)
            row.draft_title,row.draft_mode,row.draft_content=title,mode,content
            row.draft_version += 1
            row.save(update_fields=['draft_title','draft_mode','draft_content','draft_version','updated_at'])
        return {'id':row.pk,'marker':actor.marker,'created':True}


def submit_material(request, marker, id, expected_version, expected_policy, data=None):
    """Сохранить текущий payload и отправить одну неизменную версию в одной транзакции.

    Внутренний legacy caller без data явно отправляет уже сохранённый черновик.
    HTTP writers всегда передают data: отсутствие видимого текста не подменяется БД.
    Пара (material, expected_version) — ключ операции; повтор обязан совпасть
    с title/mode/content и версией принятых условий. Устаревший ключ не пишет.
    """
    with transaction.atomic():
        guard=lock_guard(); actor=_actor(request,marker,guard,'student')
        row=material_scope(actor,actor.checked_at).select_for_update().filter(pk=id).first()
        if not row: raise AccessDenied('not_found',404)
        actor=_actor(request,marker,guard,'student')
        _task_access(actor,row)
        if not assignment_scope(actor,actor.checked_at).filter(pk=row.assignment_id).exists():
            raise MaterialError('Приём по этому назначению завершён. Сохранённые материалы доступны в истории.',status=409)
        expected=version(expected_version)
        expected_policy_version=version(expected_policy)
        if data is None:
            title,mode,content=row.draft_title,row.draft_mode,row.draft_content
        else:
            if any(field not in data for field in ('title','mode','content')):
                raise MaterialError('Передайте текущие название, формат и содержание. Сохранённый текст не был отправлен.',status=400)
            title,mode,content=_draft(data)
        submitted_version=expected+(2 if data is not None else 1)
        if not row.has_draft and row.draft_version==submitted_version:
            submitted=row.revisions.select_related('policy').filter(number=row.submission_count).first()
            if (submitted and submitted.policy.version==expected_policy_version
                    and (submitted.title,submitted.mode,submitted.content)==(title,mode,content)):
                return {'id':row.pk,'marker':actor.marker,'created':False}
            raise MaterialError('С этой версией уже отправлен другой текст. Проверьте отправленную работу; текущий ввод не отправлен.',status=409)
        if row.draft_version != expected or not row.has_draft:
            raise MaterialError('Материал уже изменился. Проверьте сохранённую версию.',status=409)
        task_revision=None;task_conditions={};submitted_at=timezone.now()
        if row.task_id:
            from workspaces.teaching_services import task_submission
            task_revision,task_conditions=task_submission(actor,row,(data or {}).get('expected_task_revision'),now=submitted_at)
        policy=current_policy(row.assignment_id,row.task_id)
        if policy is None or policy.version != expected_policy_version:
            raise MaterialError('Условия приёма изменились. Проверьте их и повторите отправку.',status=409)
        content=validate_submission(mode,content,policy)
        from .attachments import validate_set
        from .models import RevisionAttachment
        files=list(row.draft_files.select_related('attachment').order_by('position'))
        validate_set(policy,[{'kind':f.attachment.kind,'size':f.attachment.size} for f in files])
        if data is not None:
            # Существующий DB trigger требует точного совпадения revision с draft
            # уже в момент INSERT. Оба UPDATE и INSERT входят в общий atomic.
            row.draft_title,row.draft_mode,row.draft_content=title,mode,content
            row.draft_version+=1
            row.save(update_fields=['draft_title','draft_mode','draft_content','draft_version','updated_at'])
        revision=MaterialRevision.objects.create(material=row,number=row.submission_count+1,
            title=title,mode=mode,content=content,policy=policy,task_revision=task_revision,task_conditions=task_conditions,submitted_at=submitted_at,attachment_manifest=[str(f.attachment_id) for f in files])
        RevisionAttachment.objects.bulk_create([RevisionAttachment(revision=revision,attachment_id=f.attachment_id,position=i) for i,f in enumerate(files)])
        row.draft_title,row.draft_mode,row.draft_content=title,mode,content
        row.submission_count=revision.number; row.has_draft=False; row.draft_version+=1
        row.save(update_fields=['draft_title','draft_mode','draft_content','submission_count','has_draft','draft_version','updated_at'])
        from workspaces.common import emit
        emit('submission',submission=revision)
        return {'id':row.pk,'marker':actor.marker,'created':True}


def start_correction(request, marker, id, expected_version):
    with transaction.atomic():
        guard=lock_guard(); actor=_actor(request,marker,guard,'student')
        row=material_scope(actor,actor.checked_at).select_for_update().filter(pk=id).first()
        if not row: raise AccessDenied('not_found',404)
        actor=_actor(request,marker,guard,'student')
        _task_access(actor,row)
        if row.has_draft or row.draft_version != version(expected_version):
            raise MaterialError('Откройте актуальный черновик.',status=409)
        latest=row.revisions.order_by('-number').first()
        if not latest: raise MaterialError('Нет отправленной версии для исправления.',status=409)
        row.draft_title,row.draft_mode,row.draft_content=latest.title,latest.mode,latest.content
        row.has_draft=True; row.draft_version+=1
        row.save(update_fields=['draft_title','draft_mode','draft_content','has_draft','draft_version','updated_at'])
        from .models import DraftAttachment
        DraftAttachment.objects.filter(material=row).delete()
        DraftAttachment.objects.bulk_create([DraftAttachment(material=row,attachment_id=f.attachment_id,position=f.position) for f in latest.files.all()])
        return {'id':row.pk,'marker':actor.marker}


def review_revision(request, marker, revision_id, data):
    with transaction.atomic():
        guard=lock_guard(); actor=_actor(request,marker,guard,'teacher')
        row=revision_scope(actor,actor.checked_at).select_for_update().filter(pk=revision_id).first()
        if not row: raise AccessDenied('not_found',404)
        actor=_actor(request,marker,guard,'teacher')
        if not revision_scope(actor,actor.checked_at).filter(pk=row.pk).exists():
            raise AccessDenied('not_found',404)
        status=data.get('status','')
        if status not in REVIEW_LABELS: raise MaterialError('Выберите результат проверки.',field='status')
        feedback=text(data.get('feedback',''),field='feedback',limit=2000,required=True)
        result=data.get('result','')
        mode=row.material.task.publication.published_revision.payload['assessment_mode'] if row.material.task_id else 'none'
        if result not in {'none':('',),'numeric':('','2','3','4','5'),'pass_fail':('','pass','fail')}[mode]:
            raise MaterialError('Результат не соответствует шкале задания.',field='result')
        existing=MaterialReview.objects.filter(revision=row).first()
        if existing:
            if existing.teacher_id!=actor.identity.pk or existing.status!=status or existing.feedback!=feedback or existing.result!=result:
                raise MaterialError('Отзыв к этой версии уже сохранён и не перезаписывается.',status=409)
        else:
            review=MaterialReview.objects.create(revision=row,teacher=actor.identity,status=status,feedback=feedback,result=result)
            from workspaces.common import emit
            emit('review',review=review)
        return {'id':row.material_id,'marker':actor.marker}


def update_policy(request, marker, assignment_id, data):
    with transaction.atomic():
        guard=lock_guard(); actor=_actor(request,marker,guard,'teacher')
        assignment=assignment_scope(actor,actor.checked_at).select_for_update(of=('self',)).filter(pk=assignment_id).first()
        if not assignment: raise AccessDenied('not_found',404)
        actor=_actor(request,marker,guard,'teacher')
        if not assignment_scope(actor,actor.checked_at).filter(pk=assignment.pk).exists():
            raise AccessDenied('not_found',404)
        old=current_policy(assignment.pk)
        expected=data.get('expected_version','0')
        if str(old.version if old else 0)!=str(expected):
            raise MaterialError('Условия уже изменились. Откройте актуальную версию.',status=409)
        modes=data.getlist('modes') if hasattr(data,'getlist') else data.get('modes',[])
        hosts=data.getlist('cloud_hosts') if hasattr(data,'getlist') else data.get('cloud_hosts',[])
        if not isinstance(modes,list) or not modes or any(mode not in MODE_LABELS for mode in modes):
            raise MaterialError('Выберите хотя бы один допустимый формат.',field='modes')
        if not isinstance(hosts,list) or any(host not in CLOUD_HOSTS for host in hosts) or ('link' in modes and not hosts):
            raise MaterialError('Выберите разрешённые облачные сервисы.',field='cloud_hosts')
        try: limit=int(data.get('text_limit',5000))
        except (TypeError,ValueError): limit=0
        if not 100<=limit<=20000: raise MaterialError('Лимит текста — от 100 до 20 000 символов.',field='text_limit')
        instructions=text(data.get('instructions',''),field='instructions',limit=500)
        from .uploads import KINDS
        from django.conf import settings
        allowed=data.get('attachments_allowed') in (True,'on','true','1')
        types=data.getlist('attachment_types') if hasattr(data,'getlist') else data.get('attachment_types',[])
        if not isinstance(types,list) or any(t not in KINDS for t in types) or allowed and not types:
            raise MaterialError('Выберите допустимые типы вложений.',field='attachment_types')
        try:
            count=int(data.get('attachment_count',settings.MATERIAL_ATTACHMENT_COUNT))
            per=int(data.get('attachment_file_mib',settings.MATERIAL_ATTACHMENT_FILE_BYTES//1024//1024))*1024*1024
            total=int(data.get('attachment_total_mib',settings.MATERIAL_ATTACHMENT_TOTAL_BYTES//1024//1024))*1024*1024
        except (ValueError,TypeError): raise MaterialError('Проверьте лимиты вложений.',field='attachment_count')
        if not (1<=count<=settings.MATERIAL_ATTACHMENT_COUNT and 1<=per<=settings.MATERIAL_ATTACHMENT_FILE_BYTES and 1<=total<=settings.MATERIAL_ATTACHMENT_TOTAL_BYTES):
            raise MaterialError('Лимиты не должны превышать ограничения прототипа.',field='attachment_count')
        SubmissionPolicy.objects.create(assignment=assignment,version=(SubmissionPolicy.objects.filter(assignment=assignment).aggregate(n=Max('version'))['n'] or 0)+1,
            modes=sorted(set(modes)),cloud_hosts=sorted(set(hosts)),text_limit=limit,instructions=instructions,author_id=actor.user_id,
            attachments_allowed=allowed,attachment_types=sorted(set(types)),attachment_count=count,
            attachment_file_limit=per,attachment_total_limit=total)
        return {'id':assignment.pk,'marker':actor.marker}


def set_assignment_active(assignment_id, active, *, operator):
    """Явная операторская операция; никаких ролей/назначений из student form."""
    if not operator or not isinstance(active,bool): raise ValueError('Укажите оператора и явный active.')
    with transaction.atomic():
        guard=lock_guard()
        row=TeachingAssignment.objects.select_for_update().get(pk=assignment_id)
        if row.active==active: return False
        row.active=active; row.save(update_fields=['active'])
        identity=Identity.objects.select_for_update().get(pk=row.teacher_id)
        identity.access_revision+=1; identity.save(update_fields=['access_revision'])
        guard.revision+=1; guard.save(update_fields=['revision'])
        # Новый marker немедленно скрывает старые открытые страницы всех затронутых ролей.
        AccessAudit.objects.create(user_id=identity.user_id,action='teaching_scope',revision=guard.revision,operator=operator)
        return True
