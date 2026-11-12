"""Явное локальное согласие вымышленного владельца; обычные profile/verify services.

Не вызывается из HTTP, login или startup. Пароли недоступных для входа classmates
не меняются. Повторная инициализация не отменяет последующее решение владельца.
"""
from contextlib import contextmanager
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit
from django.conf import settings
from django.contrib.sessions.backends.db import SessionStore
from django.db import transaction
from django.http import HttpRequest
from django.test.utils import override_settings
from django.utils import timezone
from accounts.access import current_actor, lock_guard, session_digest, logout_session
from accounts.models import AccessAudit, Identity, SessionContext
from notifications.delivery import deliver_pending
from notifications.models import EmailOutbox
from .demo_directory import _check_members
from .forms import ProfileForm
from .models import Preferences
from .services import update_profile, verify_email
from studies.models import StudentProfile
from data_import.contracts import DATASET_ID

EXAMPLES=(
    ('SP-T11-DARIA','classmate_daria','daria.volkova@example.test','+447700900101',True,True,'context'),
    ('SP-T11-KIRILL','classmate_kirill','','+447700900102',False,True,'context'),
    ('SP-T11-SOFIA','classmate_sofia','sofia.lebedeva@example.test','+447700900103',False,False,'private'),
)
ACTION='task14_demo_owner_consent'

@contextmanager
def simulated_owner(identity):
    # No password or public authentication bypass: short-lived local command session.
    request=HttpRequest();request.method='POST';request.session=SessionStore()
    request.session['_auth_user_id']=str(identity.user_id)
    request.session['_auth_user_backend']='django.contrib.auth.backends.ModelBackend'
    request.session['_auth_user_hash']=identity.user.get_session_auth_hash()
    request.session.save();now=timezone.now()
    context=SessionContext.objects.create(user_id=identity.user_id,
        session_digest=session_digest(request.session.session_key),authenticated_at=now,expires_at=now+timedelta(hours=2))
    request.session['context_id']=str(context.pk);request.session.save()
    try:yield request
    finally:logout_session(request,current_actor(request).marker)

def initial_preferences(pref,audience):
    return (pref and pref.version==1 and pref.photo_audience==audience and not pref.contact_email
        and not pref.directory_phone and not pref.share_email and not pref.share_phone
        and not pref.email_enabled and pref.email_verified_at is None and pref.email_revision==1
        and pref.email_requests and pref.email_academic and pref.email_announcements)

def provision_contacts(*,operator,apply=False,capture_dir=None):
    if not operator.strip() or len(operator)>100:raise ValueError('Укажите оператора до 100 символов.')
    capture=Path(capture_dir) if capture_dir is not None else Path(settings.BASE_DIR)/'.local/task14-protected/email'
    if apply and not capture.parent.is_dir():raise ValueError('Сначала создайте защищённую копию в .local/task14-protected.')
    results=[]
    with transaction.atomic():
        guard=lock_guard()
        owner=StudentProfile.objects.select_related('group','dataset').get(dataset_id=DATASET_ID,source_id='SP-A')
        _check_members(owner)
        for source_id,username,email,phone,share_email,share_phone,audience in EXAMPLES:
            identity=Identity.objects.select_related('user','profile').get(profile__source_id=source_id,user__username=username,role='student')
            if identity.user.has_usable_password():raise ValueError('Демо-владелец получил пароль; автоматическая инициализация запрещена.')
            if AccessAudit.objects.filter(user_id=identity.user_id,action=ACTION).exists():
                results.append({'identity':username,'outcome':'noop'});continue
            pref=Preferences.objects.select_for_update().filter(user_id=identity.user_id).first()
            if not initial_preferences(pref,audience):
                results.append({'identity':username,'outcome':'conflict_preserved'});continue
            result={'identity':username,'outcome':'initialized' if apply else 'preview','email':email,'phone':phone,
                'share_email':share_email,'share_phone':share_phone,'consent':'simulated_demo_owner'}
            results.append(result)
            if not apply:continue
            capture.mkdir(exist_ok=True)
            with simulated_owner(identity) as request:
                def save(**changes):
                    nonlocal pref
                    pref.refresh_from_db()
                    data={f.name:getattr(pref,f.name) for f in pref._meta.fields if f.name not in ('id','user','email_verified_at','email_revision')}
                    data.update(changes)
                    form=ProfileForm(data,instance=pref)
                    if not form.is_valid():raise ValueError('Демо-профиль не прошёл обычную валидацию.')
                    pref=update_profile(request,current_actor(request).marker,form.cleaned_data)
                save(contact_email=email,directory_phone=phone,share_phone=share_phone)
                if share_email:
                    row=EmailOutbox.objects.get(user_id=identity.user_id,purpose='verification',email_revision=pref.email_revision,address=email)
                    previous=set(capture.glob('*.eml'))
                    with override_settings(EMAIL_BACKEND='notifications.mail_backend.CaptureBackend',EMAIL_FILE_PATH=str(capture),MAIL_EXTERNAL_ENABLED=False):
                        stats=deliver_pending(limit=1,verification_key=row.key)
                    if stats['captured']!=1:raise ValueError('Local capture не подтверждён.')
                    files=set(capture.glob('*.eml'))-previous
                    if len(files)!=1:raise ValueError('Ожидалось ровно одно локальное письмо.')
                    message=BytesParser(policy=policy.default).parsebytes(files.pop().read_bytes())
                    link=re.search(r'https?://\S+',message.get_content()).group(0)
                    token=parse_qs(urlsplit(link).query)['token'][0]
                    verify_email(request,current_actor(request).marker,token)
                    save(share_email=True)
                guard.refresh_from_db()
                AccessAudit.objects.create(user_id=identity.user_id,action=ACTION,revision=guard.revision,operator=operator)
    return results
