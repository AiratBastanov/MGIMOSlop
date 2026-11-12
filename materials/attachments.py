"""Private immutable bytes, exact revision sets and idempotent editor operations."""
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from django.conf import settings
from django.db import transaction
from accounts.access import AccessDenied, current_actor, lock_guard
from . import services
from .models import Material, MaterialAttachment, DraftAttachment, RevisionAttachment, MaterialOperation
from .selectors import material_scope, current_policy
from .uploads import KINDS, safe_name


def limits(policy):
    return (min(policy.attachment_count,settings.MATERIAL_ATTACHMENT_COUNT),
            min(policy.attachment_file_limit,settings.MATERIAL_ATTACHMENT_FILE_BYTES),
            min(policy.attachment_total_limit,settings.MATERIAL_ATTACHMENT_TOTAL_BYTES))


def validate_set(policy, rows):
    if not rows: return
    if not policy or not policy.attachments_allowed:
        raise services.MaterialError('Преподаватель не разрешил вложения в текущих условиях.',field='attachments')
    count,per_file,total=limits(policy)
    if len(rows)>count or sum(r['size'] for r in rows)>total:
        raise services.MaterialError(f'Допустимо до {count} файлов и {total/1024/1024:g} МиБ вместе.',field='attachments')
    if any(r['kind'] not in policy.attachment_types or r['size']>per_file for r in rows):
        raise services.MaterialError(f'Проверьте разрешённые типы и лимит {per_file/1024/1024:g} МиБ на файл.',field='attachments')


def inspect_upload(upload):
    try: name,kind=safe_name(upload.name)
    except ValueError: raise services.MaterialError('Недопустимое имя или тип файла.',field='attachments')
    parts=[];size=0
    for chunk in upload.chunks(64*1024):
        size+=len(chunk)
        if size>settings.MATERIAL_ATTACHMENT_FILE_BYTES:
            raise services.MaterialError('Файл превышает лимит размера.',field='attachments')
        parts.append(chunk)
    raw=b''.join(parts)
    try:
        result=subprocess.run([sys.executable,str(Path(__file__).with_name('file_inspector.py')),kind],
            input=raw,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        if result.returncode or result.stdout!=b'valid': raise ValueError('content')
    except (ValueError,OSError,subprocess.TimeoutExpired):
        raise services.MaterialError('Файл повреждён, содержит неподдерживаемые возможности или не соответствует своему типу.',field='attachments')
    return {'name':name,'kind':kind,'mime':KINDS[kind],'size':size,'sha256':hashlib.sha256(raw).hexdigest(),'data':raw}


def perform(request,marker,data,material_id):
    """One transaction for text, all files, immutable submission and receipt."""
    actor=current_actor(request,expected=marker);services.role(actor,'student')
    if getattr(request,'material_upload_error',False):
        raise services.MaterialError('Превышены лимиты загрузки или передано неизвестное поле файла.',field='attachments')
    if set(request.FILES)-{'files'}: raise services.MaterialError('Неизвестное поле файла.',field='attachments')
    files=request.FILES.getlist('files')
    if len(files)>settings.MATERIAL_ATTACHMENT_COUNT: raise services.MaterialError('Слишком много файлов.',field='attachments')
    try:
        key=uuid.UUID(str(data.get('operation','')))
        retained=json.loads(data.get('attachment_ids','[]'))
        if not isinstance(retained,list) or len(retained)>5 or len(set(retained))!=len(retained): raise ValueError('ids')
        retained=[str(uuid.UUID(v)) for v in retained]
        if len(set(retained))!=len(retained): raise ValueError('duplicate canonical ids')
    except (ValueError,TypeError,AttributeError):
        raise services.MaterialError('Неверный ключ операции или набор вложений. Откройте актуальный редактор.',status=409)
    uploads=[inspect_upload(f) for f in files]  # EVERY item, before any durable mutation.
    action=data.get('action','')
    fields={k:data.get(k,'') for k in ('action','assignment','expected_version','expected_policy','title','mode','content','task','expected_task_revision')}
    fingerprint=hashlib.sha256(json.dumps({'fields':fields,'target':material_id,'retained':retained,
        'files':[{k:v for k,v in row.items() if k!='data'} for row in uploads]},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    with transaction.atomic():
        guard=lock_guard();actor=services._actor(request,marker,guard,'student')
        row=None
        if material_id is not None:
            row=material_scope(actor,actor.checked_at).select_for_update().filter(pk=material_id).first()
            if not row: raise AccessDenied('not_found',404)
        actor=services._actor(request,marker,guard,'student')
        if row and row.task_id:
            from workspaces.selectors import task_scope
            if not task_scope(actor,actor.checked_at).filter(pk=row.task_id).exists(): raise AccessDenied('not_found',404)
        receipt=MaterialOperation.objects.filter(owner_id=actor.profile_id,key=key).first()
        if receipt:
            if receipt.fingerprint!=fingerprint: raise services.MaterialError('С этим ключом уже сохранён другой набор данных.',status=409)
            if not material_scope(actor,actor.checked_at).filter(pk=receipt.material_id).exists(): raise AccessDenied('not_found',404)
            if receipt.material.task_id:
                from workspaces.selectors import task_scope
                if not task_scope(actor,actor.checked_at).filter(pk=receipt.material.task_id).exists(): raise AccessDenied('not_found',404)
            return {'id':receipt.material_id,'marker':actor.marker,'created':False}
        if row and row.draft_version!=services.version(data.get('expected_version')):
            raise services.MaterialError('Черновик изменился. Проверьте актуальную версию.',status=409)
        if action=='correct':
            if not row or files or retained: raise services.MaterialError('Откройте исправление отдельным действием.',status=409)
            result=services.start_correction(request,marker,row.pk,data.get('expected_version'))
        else:
            if action not in ('save','submit') or (action=='submit' and not row): raise services.MaterialError('Выберите действие в редакторе.')
            if row and not row.has_draft: raise services.MaterialError('Сначала откройте исправление.',status=409)
            old=list(MaterialAttachment.objects.filter(material=row,id__in=retained).defer('data')) if row else []
            by_id={str(a.pk):a for a in old}
            if set(by_id)!=set(retained): raise AccessDenied('not_found',404)
            if row:
                allowed=set(str(v) for v in DraftAttachment.objects.filter(material=row).values_list('attachment_id',flat=True))
                if not set(retained)<=allowed: raise AccessDenied('not_found',404)
            if action=='save':
                result=services.save_draft(request,marker,data,material_id)
                row=Material.objects.get(pk=result['id'])
            policy=current_policy(row.assignment_id,row.task_id)
            if (files or retained) and (not policy or str(policy.version)!=str(data.get('expected_policy'))):
                raise services.MaterialError('Условия приёма изменились. Проверьте их перед загрузкой.',status=409)
            descriptors=[{'kind':by_id[v].kind,'size':by_id[v].size} for v in retained]+uploads
            validate_set(policy,descriptors)
            selected=[by_id[v] for v in retained]+[MaterialAttachment.objects.create(material=row,**v) for v in uploads]
            DraftAttachment.objects.filter(material=row).delete()
            DraftAttachment.objects.bulk_create([DraftAttachment(material=row,attachment=a,position=i) for i,a in enumerate(selected)])
            if action=='submit':
                result=services.submit_material(request,marker,row.pk,data.get('expected_version'),data.get('expected_policy'),data)
            # Only unreferenced bytes of this owned material. Submitted history is protected.
            MaterialAttachment.objects.filter(material=row,draftattachment__isnull=True,revisionattachment__isnull=True).delete()
        MaterialOperation.objects.create(owner_id=actor.profile_id,key=key,fingerprint=fingerprint,material_id=result['id'],action=action)
        return result


def file_values(attachment,revision_id=None):
    prefix=f'/study/materials/revisions/{revision_id}/files/' if revision_id else '/study/materials/files/'
    return {'id':str(attachment.pk),'name':attachment.name,'kind':attachment.kind,'mime':attachment.mime,
        'size':attachment.size,'sha256':attachment.sha256,'url':f'{prefix}{attachment.pk}/'}
