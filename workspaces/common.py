import hashlib
import json
import uuid
from datetime import datetime
from urllib.parse import urlsplit
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.utils import timezone
from data_import.contracts import MOSCOW
from materials.services import MaterialError, text, version
from .models import WorkspaceOperation, ApplicationEvent


def https_link(value, field='link'):
    value=text(value,field=field,limit=1000)
    if value:
        try:
            URLValidator(schemes=['https'])(value)
            parsed=urlsplit(value)
            if parsed.username or parsed.password or parsed.port not in (None,443):
                raise ValueError()
        except (ValidationError,ValueError):
            raise MaterialError('Нужна HTTPS-ссылка без логина, пароля и нестандартного порта.',field=field)
    return value


def instant(value, field):
    if not value: return None
    try:
        result=datetime.fromisoformat(value)
        if timezone.is_naive(result): result=result.replace(tzinfo=MOSCOW)
        return result.isoformat()
    except (ValueError,TypeError):
        raise MaterialError('Укажите дату и время по Москве.',field=field)


def operation(actor, data, *, target, files=()):
    try: key=uuid.UUID(str(data.get('operation','')))
    except (ValueError,TypeError): raise MaterialError('Откройте редактор заново: ключ операции отсутствует.',status=409)
    fields={k:(data.getlist(k) if hasattr(data,'getlist') else data[k]) for k in data if k not in ('context','csrfmiddlewaretoken','operation')}
    digest=hashlib.sha256(json.dumps({'target':target,'fields':fields,'files':[{k:v for k,v in item.items() if k!='data'} for item in files]},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    old=WorkspaceOperation.objects.filter(user_id=actor.user_id,key=key).first()
    if old and old.fingerprint!=digest:
        raise MaterialError('Этот ключ уже использован с другими данными. Проверьте сохранённый результат.',status=409)
    return key,digest,old.result if old else None


def record_operation(actor,key,digest,result):
    WorkspaceOperation.objects.create(user_id=actor.user_id,key=key,fingerprint=digest,result=result)
    return {**result,'marker':actor.marker}


def emit(kind, **target):
    from notifications.models import Notification
    event=ApplicationEvent.objects.create(kind=kind,**target)
    Notification.objects.create(application_event=event,published_at=event.created_at)
    return event


def bounded_int(data,key,default,minimum,maximum):
    raw=data.get(key,default)
    if isinstance(raw,bool) or not str(raw).isdigit() or not minimum<=int(raw)<=maximum:
        raise MaterialError(f'Допустимо целое число от {minimum} до {maximum}.',field=key)
    return int(raw)
