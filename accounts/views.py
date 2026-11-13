from functools import wraps
from datetime import date
import re
import uuid
from urllib.parse import urlencode, urlsplit, parse_qs
from django import forms
from django.db import DatabaseError, transaction
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST, require_http_methods
from django.views.decorators.debug import sensitive_post_parameters
from .access import AccessDenied, authenticate_session, current_actor, logout_session, lock_guard
from .selectors import private_snapshot
from notifications.actions import mark_read
from . import components


def public_error(request, status=503, message=None):
    messages = {400:'Проверьте дату и выбранный режим расписания.',403:'Это действие недоступно для вашей учётной записи.',404:'Страница не найдена.',
                409:'Сеанс или доступ изменился. Откройте свежую страницу и повторите действие.',
                503:'Кабинет временно недоступен. Попробуйте ещё раз чуть позже.'}
    return render(request,'error.html',{'message':message or messages.get(status,messages[503])},status=status)


def protected(view):
    @wraps(view)
    def wrapped(request,*args,**kwargs):
        try:
            return view(request,*args,**kwargs)
        except AccessDenied as exc:
            if request.path == '/context/' or components.enhanced(request):
                return JsonResponse({'state':exc.code},status=exc.status)
            if exc.status == 401:
                return redirect('/login/?' + urlencode({'next':request.get_full_path(),'expired':'1'}))
            return public_error(request,exc.status)
        except DatabaseError:
            if request.path == '/context/' or components.enhanced(request): return JsonResponse({'state':'unavailable'},status=503)
            return public_error(request)
    return wrapped


def safe_next(value, role):
    fallback = {'student':'/today/','staff':'/staff/','teacher':'/teacher/'}[role]
    if not isinstance(value,str) or not value.startswith('/') or value.startswith('//') or '\\' in value or len(value) > 300:
        return fallback
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment or any(ord(char)<32 for char in value):
        return fallback
    path = parsed.path
    if path=='/search/' and not parsed.query:
        return value
    if role=='student' and re.fullmatch(r'/journeys/(?:[1-9][0-9]*/)?',path) and set(parse_qs(parsed.query))<={'template'}:
        return value
    if re.fullmatch(r'/consultations/(?:bookings/[1-9][0-9]*/)?',path) and set(parse_qs(parsed.query))<={'assignment','course','request','slot','page','booking_page','rebook','journey'}:
        return value
    if path=='/notifications/':
        try:validate_notification_params(parse_qs(parsed.query,keep_blank_values=True,strict_parsing=True))
        except (ValueError,TypeError):return fallback
        return value
    if role=='teacher' and re.fullmatch(r'/teacher/(?:courses/|review/|schedule/|publications/new/|(?:courses|publications)/[1-9][0-9]*/)',path) and set(parse_qs(parsed.query))<={'kind','assignment','tab','course','group','task','state','page','date','view'}:
        return value
    if role=='staff' and re.fullmatch(r'/staff/(?:requests|groups|notices|resources)/(?:new/|[1-9][0-9]*/)?',path) and set(parse_qs(parsed.query))<={'q','type','group','clarification','status','page'}:
        return value
    if role=='student' and (re.fullmatch(r'/study/teaching/(?:[1-9][0-9]*/)?',path) or re.fullmatch(r'/notices/[1-9][0-9]*/',path)) and set(parse_qs(parsed.query))<={'kind','page'}:
        return value
    if path in ('/profile/edit/', '/profile/verify-email/') and (not parsed.query or (path == '/profile/verify-email/' and set(parse_qs(parsed.query)) == {'token'})):
        return value
    if role == 'teacher':
        if path in ('/teacher/', '/profile/') or re.fullmatch(r'/teacher/(?:materials|assignments)/[1-9][0-9]*/',path):
            return path
        return fallback
    if role != 'student':
        if value in ('/staff/', '/profile/'):
            return value
        if re.fullmatch(r'/staff/requests/[1-9][0-9]*/',path) and not parsed.query:
            return path
        if path == '/staff/' and set(parse_qs(parsed.query)) <= {'status','page'}:
            return value
        if re.fullmatch(r'/staff/resources/(?:[1-9][0-9]*/)?',path) and not parsed.query:
            return path
        return fallback
    if path == '/requests/' and set(parse_qs(parsed.query)) <= {'status','page'}:
        return value
    if path == '/requests/new/' and set(parse_qs(parsed.query)) <= {'type'}:
        return value
    if path in ('/profile/group/', '/study/materials/', '/study/materials/new/') and not parsed.query:
        return path
    if re.fullmatch(r'/study/materials/[1-9][0-9]*/',path) and not parsed.query:
        return path
    if path in ('/requests/', '/requests/new/') and not parsed.query:
        return path
    if path in ('/today/','/profile/') and not parsed.query:
        return path
    if re.fullmatch(r'/(?:requests|lessons|announcements|notifications|assessments|study/enrollments)/[1-9][0-9]*/',path) and not parsed.query:
        return path
    if path == '/notifications/':
        try:
            validate_notification_params(parse_qs(parsed.query,keep_blank_values=True,strict_parsing=True))
        except (ValueError,TypeError):
            return fallback
        return value
    if path == '/schedule/':
        try:
            params = parse_qs(parsed.query,keep_blank_values=True,strict_parsing=True)
            validate_schedule_params(params)
        except (ValueError,TypeError):
            return fallback
        return value
    if path == '/study/':
        try:
            validate_study_params(parse_qs(parsed.query,keep_blank_values=True,strict_parsing=True))
        except (ValueError,TypeError):
            return fallback
        return value
    return fallback


def validate_schedule_params(params):
    """URL содержит только выбранные дату/режим; ни owner, ни scope из него не берутся."""
    if set(params) - {'date','view'} or any(len(values) != 1 for values in params.values()):
        raise ValueError('query')
    mode = params.get('view',['day'])[0]
    if mode not in ('day','week'):
        raise ValueError('mode')
    raw = params.get('date',[None])[0]
    selected = None
    if raw is not None:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw):
            raise ValueError('date')
        selected = date.fromisoformat(raw)
        # Не позволяем пред/след навигации переполнить datetime.date на краях.
        if not 1901 <= selected.year <= 2099:
            raise ValueError('date_range')
    return selected,mode


def validate_study_params(params):
    if set(params) - {'period','view','filter'} or any(len(values) != 1 for values in params.values()):
        raise ValueError('query')
    view = params.get('view',['results'])[0]
    filter_name = params.get('filter',['all'])[0]
    period = params.get('period',[None])[0]
    if view not in ('results','assessments') or filter_name not in ('all','open','overdue','unentered','assessed'):
        raise ValueError('mode')
    if view == 'assessments' and filter_name != 'all':
        raise ValueError('filter')
    if period is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}',period):
        raise ValueError('period')
    return period,view,filter_name


def validate_notification_params(params):
    if set(params) - {'archive','kind','page','undo','mode'} or any(len(values) != 1 for values in params.values()):
        raise ValueError('query')
    archive = params.get('archive',['0'])[0]
    kind = params.get('kind',['all'])[0]
    if archive not in ('0','1') or kind not in ('all','schedule','academic','announcements','deadlines','requests'):
        raise ValueError('filter')
    if params.get('mode',['inbox'])[0] not in ('inbox','history','trash'): raise ValueError('mode')
    if 'page' in params and not re.fullmatch(r'[1-9][0-9]{0,6}',params['page'][0]):
        raise ValueError('page')
    if 'undo' in params:
        uuid.UUID(params['undo'][0])
    return archive == '1',kind


class LoginForm(forms.Form):
    username = forms.CharField(label='Логин',max_length=150,widget=forms.TextInput(attrs={'autocomplete':'username','autocapitalize':'none','spellcheck':'false'}))
    password = forms.CharField(label='Пароль',strip=False,widget=forms.PasswordInput(attrs={'autocomplete':'current-password'}))


@require_http_methods(['GET','POST'])
@sensitive_post_parameters('password')
@protected
def login_page(request):
    try:
        actor = current_actor(request)
    except AccessDenied as exc:
        if exc.status == 503: raise
        actor = None
    if actor:
        if request.method == 'POST': return public_error(request,409)
        return redirect(safe_next(request.GET.get('next'),actor.role))
    form = LoginForm(request.POST if request.method == 'POST' else None)
    error = None
    if request.method == 'POST':
        if form.is_valid():
            role = authenticate_session(request,**form.cleaned_data)
            if role: return redirect(safe_next(request.POST.get('next'),role))
        error = 'Не удалось войти. Проверьте логин и пароль.'
    return render(request,'login.html',{'form':form,'error':error,'next':request.GET.get('next',request.POST.get('next','')),
                                       'expired':request.GET.get('expired') == '1'})


@require_GET
@protected
def home(request):
    actor = current_actor(request)
    return redirect(safe_next(None,actor.role))


@require_GET
@protected
def profile(request, profile_id=None):
    from profiles.selectors import own_preferences
    data = private_snapshot(request,profile_id=profile_id,
        extension=lambda actor, now: {'preferences': own_preferences(actor)} if actor.role == 'student' else {})
    return render(request,'profile.html' if data['role'] == 'student' else 'staff_profile.html',data)


@require_GET
@protected
def staff(request):
    data = private_snapshot(request,section='staff')
    if data['role'] != 'staff': raise AccessDenied('forbidden',403)
    return render(request,'staff.html',data)


@require_GET
@protected
def notifications(request, notification_id=None):
    if components.enhanced(request):
        current_actor(request, expected=request.headers.get('X-Page-Context',''))
    if request.GET.get('mode') == 'trash' or 'undo' in request.GET:
        if current_actor(request).role != 'student':
            raise AccessDenied('forbidden',403)
        return redirect('/notifications/?mode=history')
    try:
        archive,kind = validate_notification_params(dict(request.GET.lists()))
    except (ValueError,TypeError):
        # Проверка identity сохраняется даже для испорченных параметров.
        if current_actor(request).role != 'student':
            raise AccessDenied('forbidden',403)
        return public_error(request,400,'Неизвестный режим уведомлений. Откройте активную ленту.')
    data = private_snapshot(request,section='notifications', notification_id=notification_id,
        archive=archive,inbox_mode=request.GET.get('mode'),notification_kind=kind,notification_page_number=request.GET.get('page',1))
    data['component_data'] = components.inbox_data(request,data)
    if components.enhanced(request):
        return components.component_response(request,data['component_data'])
    return render(request,'notifications.html',data)


@require_GET
@protected
def today(request):
    return render(request,'today.html',private_snapshot(request,section='today'))


@require_GET
@protected
def schedule(request):
    if components.enhanced(request):
        current_actor(request, expected=request.headers.get('X-Page-Context',''))
    try:
        selected,mode = validate_schedule_params(dict(request.GET.lists()))
    except (ValueError,TypeError):
        if current_actor(request).role != 'student':
            raise AccessDenied('forbidden',403)
        data = private_snapshot(request,section='schedule')
        data.update(date_error='Проверьте дату и режим расписания.',
                    attempted_date=request.GET.get('date',''),date_form_open=True)
        data['component_data'] = components.schedule_data(request,data)
        if components.enhanced(request):
            return JsonResponse({'state':'invalid','error':data['date_error'],'marker':data['marker']},status=400)
        return render(request,'schedule.html',data,status=400)
    data = private_snapshot(request,section='schedule',selected_date=selected,mode=mode)
    data['component_data'] = components.schedule_data(request,data)
    if components.enhanced(request):
        return components.component_response(request,data['component_data'])
    return render(request,'schedule.html',data)


@require_GET
@protected
def study(request):
    try:
        period,view,filter_name = validate_study_params(dict(request.GET.lists()))
    except (ValueError,TypeError):
        if current_actor(request).role != 'student':
            raise AccessDenied('forbidden',403)
        return public_error(request,400,'Неизвестный период или фильтр. Откройте раздел «Учёба».')
    from studies.routes import ALL_ASSESSED_RESULTS_URL
    return render(request,'study.html',{**private_snapshot(request,section='study',period_id=period,
        study_mode=view,study_filter=filter_name), 'all_assessed_results_url': ALL_ASSESSED_RESULTS_URL})


@require_GET
@protected
def enrollment_detail(request, enrollment_id):
    if request.GET:
        if current_actor(request).role != 'student':
            raise AccessDenied('forbidden',403)
        return public_error(request,400,'Параметры страницы не поддерживаются.')
    return render(request,'enrollment_detail.html',private_snapshot(request,section='enrollment',enrollment_id=enrollment_id))


@require_GET
@protected
def assessment_detail(request, assessment_id):
    if request.GET:
        if current_actor(request).role != 'student':
            raise AccessDenied('forbidden',403)
        return public_error(request,400,'Параметры страницы не поддерживаются.')
    return render(request,'assessment_detail.html',private_snapshot(request,section='assessment',assessment_id=assessment_id))


@require_GET
@protected
def lesson_detail(request, lesson_id):
    return render(request,'lesson_detail.html',private_snapshot(request,section='lesson',lesson_id=lesson_id))


@require_GET
@protected
def announcement_detail(request, announcement_id):
    return render(request,'announcement_detail.html',private_snapshot(request,section='announcement',announcement_id=announcement_id))


@require_GET
@protected
def context(request):
    with transaction.atomic():
        actor = current_actor(request,guard=lock_guard(),lock=True,expected=request.headers.get('X-Page-Context',''))
        data = {'marker':actor.marker,'expires_at':actor.expires_at.isoformat()}
    return JsonResponse(data)


@require_POST
@protected
def read_notification(request, notification_id):
    mark_read(request,notification_id,request.POST.get('context',''))
    if components.enhanced(request):
        return JsonResponse({'marker':current_actor(request,expected=request.POST.get('context','')).marker,'state':'saved'})
    return redirect('/notifications/')


@require_POST
@protected
def logout_page(request):
    logout_session(request,request.POST.get('context',''))
    return redirect('/login/')


def csrf_failure(request, reason=''):
    return public_error(request,403,'Не удалось проверить форму. Откройте страницу заново и повторите действие.')


def locked_out(request, original_response=None, credentials=None):
    response = render(request,'login.html',{'form':LoginForm(),'error':'Вход временно ограничен. Ожидание — до 15 минут после последней учитываемой ошибки. Повторные попытки во время ограничения не продлевают его.'},status=429)
    response['Retry-After'] = '900'
    return response


def not_found(request,exception=None): return public_error(request,404)
def server_error(request): return public_error(request,503)
