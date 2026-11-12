"""Единый протокол доступа: guard → context → target. Все проверки внутри lock.

Только этот модуль и явное provisioning меняют security state. Import меняет
учебный scope под тем же guard, но никогда не пишет identity/session.
"""
from dataclasses import dataclass
from datetime import timedelta
import hmac
from ipaddress import ip_address
import uuid
from django.conf import settings
from django.contrib.auth import authenticate, login, logout, get_user_model
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import salted_hmac, constant_time_compare
from .models import AuthorizationGuard, Identity, StaffScope, SessionContext, AccessAudit


class AccessDenied(Exception):
    def __init__(self, code='login_required', status=401):
        self.code, self.status = code, status
        super().__init__(code)


def client_ip(request):
    """Axes identity only; never session binding. One explicitly enabled proxy."""
    remote = request.META.get('REMOTE_ADDR')
    if not settings.CLIENT_IP_TRUSTED_PROXY or remote != settings.CLIENT_IP_TRUSTED_PROXY:
        return remote
    value = request.META.get('HTTP_X_DEANERY_CLIENT_IP', '')
    # A single unadorned address: no chain, port, zone ID or surrounding whitespace.
    if not isinstance(value, str) or not value or value != value.strip() or '%' in value:
        return remote
    try:
        return str(ip_address(value))
    except ValueError:
        # Missing/broken proxy metadata shares the proxy bucket, never bypasses Axes.
        return remote


def lock_guard():
    try:
        return AuthorizationGuard.objects.select_for_update().get(pk=1)
    except AuthorizationGuard.DoesNotExist:
        raise AccessDenied('not_initialized',503)


def session_digest(key):
    return salted_hmac('session-binding',key or '',algorithm='sha256').hexdigest()


def identity_for(user_id):
    user = get_user_model().objects.filter(pk=user_id,is_active=True,is_staff=False,is_superuser=False).first()
    if not user or user.groups.exists() or user.user_permissions.exists():
        raise AccessDenied()
    identity = Identity.objects.select_related('profile__group__programme__faculty', 'instructor').filter(user=user).first()
    if not identity:
        raise AccessDenied()
    if identity.role == 'student':
        if not identity.profile_id or identity.instructor_id or StaffScope.objects.filter(identity=identity).exists():
            raise AccessDenied('invalid_scope',403)
        profile = identity.profile
        if not profile.group.programme_id or not 1 <= profile.year <= 6:
            raise AccessDenied('invalid_scope',403)
        programme = profile.group.programme
        if len({profile.dataset_id, profile.group.dataset_id, programme.dataset_id, programme.faculty.dataset_id}) != 1:
            raise AccessDenied('invalid_scope',403)
    elif identity.role == 'staff':
        if identity.profile_id or identity.instructor_id or not identity.display_name or not StaffScope.objects.filter(identity=identity).exists():
            raise AccessDenied('invalid_scope',403)
    elif identity.role == 'teacher':
        if identity.profile_id or not identity.instructor_id or StaffScope.objects.filter(identity=identity).exists():
            raise AccessDenied('invalid_scope',403)
    else:
        raise AccessDenied('invalid_scope',403)
    return user, identity


@dataclass(frozen=True)
class Actor:
    user_id: int
    role: str
    profile_id: int | None
    group_id: int | None
    marker: str
    context_id: uuid.UUID
    expires_at: object
    identity: Identity
    checked_at: object


def current_actor(request, *, guard=None, lock=False, expected=None):
    uid = request.session.get('_auth_user_id')
    cid = request.session.get('context_id')
    try:
        cid = uuid.UUID(str(cid))
        uid = int(uid)
    except (TypeError,ValueError):
        raise AccessDenied()
    guard = guard or AuthorizationGuard.objects.filter(pk=1).first()
    if not guard: raise AccessDenied('not_initialized',503)
    query = SessionContext.objects.select_for_update() if lock else SessionContext.objects
    context = query.filter(pk=cid,user_id=uid,session_digest=session_digest(request.session.session_key)).first()
    checked_at = timezone.now()
    if not context or context.revoked_at is not None or checked_at >= context.expires_at:
        raise AccessDenied('session_expired')
    user, identity = identity_for(uid)  # свежий User, не cached request.user
    if not constant_time_compare(request.session.get('_auth_user_hash',''),user.get_session_auth_hash()):
        raise AccessDenied()
    profile = identity.profile if identity.role == 'student' else None
    source = f'{context.pk}:{guard.revision}:{identity.access_revision}:{profile.scope_revision if profile else 0}'
    # Teaching/photo permissions have real clock boundaries even without a write.
    # This deadline hides the current DOM; it never extends/revokes the DB session.
    privacy_until = context.expires_at
    if identity.role in ('teacher', 'student'):
        from materials.selectors import assignment_candidates
        scope_actor = Actor(uid,identity.role,identity.profile_id,profile.group_id if profile else None,
            '',context.pk,context.expires_at,identity,checked_at)
        assignments = list(assignment_candidates(scope_actor).order_by('pk')
            .values('pk','available_from','available_until'))
        current_ids = [str(row['pk']) for row in assignments
            if row['available_from'] <= checked_at < row['available_until']]
        source += ':teaching:' + ','.join(current_ids)
        boundaries = [row[field] for row in assignments for field in ('available_from','available_until')
            if checked_at < row[field] < privacy_until]
        if boundaries:
            privacy_until = min(boundaries)
    marker = salted_hmac('page-context',source,algorithm='sha256').hexdigest()
    if expected is not None and not constant_time_compare(str(expected),marker):
        raise AccessDenied('stale_context',409)
    return Actor(uid,identity.role,identity.profile_id,profile.group_id if profile else None,
                 marker,context.pk,privacy_until,identity,checked_at)


def authenticate_session(request, username, password):
    with transaction.atomic():
        guard = lock_guard()
        # Serialize authentication and Axes decisions for the small local demo.
        user = authenticate(request,username=username,password=password)
        if user is None: return None
        try:
            _, identity = identity_for(user.pk)
        except AccessDenied:
            return None
        old = SessionContext.objects.select_for_update().filter(session_digest=session_digest(request.session.session_key)).first()
        if old and old.revoked_at is None:
            old.revoked_at = timezone.now()
            old.save(update_fields=['revoked_at'])
        # Same-account reauthentication must rotate too.
        request.session.cycle_key()
        login(request,user)
        # login() может flush() старую сессию при иной identity/auth hash.
        # Привязываемся только к окончательному сохранённому ключу.
        request.session.save()
        now = timezone.now()
        expires = now + timedelta(hours=2)
        context = SessionContext.objects.create(user=user,session_digest=session_digest(request.session.session_key),
            authenticated_at=now,expires_at=expires)
        request.session['context_id'] = str(context.pk)
        request.session.set_expiry(expires)
        request.session.save()
        request.session.modified = True  # cookie only after guarded middleware check
        return identity.role


def logout_session(request, marker):
    with transaction.atomic():
        guard = lock_guard()
        actor = current_actor(request,guard=guard,lock=True,expected=marker)
        SessionContext.objects.filter(pk=actor.context_id).update(revoked_at=timezone.now())
        logout(request)


def set_active(user_id, active, *, operator):
    """Единственное включённое операторское изменение доступа в S2.

    Смена роли/StaffScope/пароля не имеет команды до отдельной реализации.
    """
    with transaction.atomic():
        guard = lock_guard()
        contexts = list(SessionContext.objects.select_for_update().filter(user_id=user_id).order_by('pk'))
        user = get_user_model().objects.select_for_update().get(pk=user_id)
        identity = Identity.objects.select_for_update().get(user=user)
        if user.is_active == active: return False
        user.is_active = active
        user.save(update_fields=['is_active'])
        identity.access_revision += 1
        identity.save(update_fields=['access_revision'])
        guard.revision += 1
        guard.save(update_fields=['revision'])
        SessionContext.objects.filter(pk__in=[x.pk for x in contexts],revoked_at__isnull=True).update(revoked_at=timezone.now())
        AccessAudit.objects.create(user=user,action='activate' if active else 'deactivate',revision=guard.revision,operator=operator)
        return True
