"""HTTP-слой заявок: авторизация и writers только через общие services/selectors."""
from functools import wraps
import re
from django.db import DatabaseError
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.crypto import constant_time_compare
from django.views.decorators.http import require_GET, require_http_methods
from django.views.decorators.debug import sensitive_post_parameters
from accounts.access import AccessDenied, current_actor
from accounts.selectors import private_snapshot
from accounts.views import protected, public_error
from .forms import RequestForm, DecisionForm
from .services import RequestError, submit_request, transition_request
from .workflow import REQUEST_TYPE, REQUEST_TYPES, PAYLOAD_FIELDS, STATUS_LABELS
from accounts.components import request_data


def enhanced(request):
    return request.headers.get('X-Request-Form') == '1'


def request_protected(view):
    @wraps(view)
    @protected
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except AccessDenied as exc:
            if enhanced(request):
                return JsonResponse({'state': exc.code}, status=exc.status)
            raise
        except DatabaseError:
            if enhanced(request):
                return JsonResponse({'state': 'save_unconfirmed'}, status=503)
            return public_error(request, 503, 'Не удалось подтвердить сохранение. Откройте кабинет и проверьте список заявок перед повторной отправкой.')
    return wrapped


def snapshot_for_form(request, section, **kwargs):
    data = private_snapshot(request, section=section, **kwargs)
    if request.method == 'POST' and not constant_time_compare(data['marker'], request.POST.get('context', '')):
        raise AccessDenied('stale_context', 409)
    return data


def page(request, template, data, status=200):
    response = render(request, template, data, status=status)
    response['X-Page-Context'] = data['marker']
    return response


def saved(request, result, url):
    # Commit уже выполнен сервисом. Старая response identity не раскрывается.
    current_actor(request, expected=result['marker'])
    if enhanced(request):
        return JsonResponse({'state': 'saved', 'marker': result['marker'], 'redirect': url})
    return redirect(url)


@require_GET
@request_protected
def request_list(request):
    if current_actor(request).role != 'student':
        raise AccessDenied('forbidden', 403)
    try:
        value = status_filter(request, default='all')
        number = page_number(request)
    except ValueError:
        return public_error(request, 400, 'Проверьте статус и номер страницы заявок.')
    data = private_snapshot(request, section='requests', request_filter=value, request_page_number=number)
    return page(request, 'requests.html', data)


@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters('comment', 'recipient', 'record_label', 'requested_change', 'submission_token', 'context')
@request_protected
def request_new(request):
    error = None
    request_type = request.POST.get('request_type', REQUEST_TYPE) if request.method == 'POST' else request.GET.get('type', REQUEST_TYPE)
    if request.method == 'GET' and request_type == 'exchange_plan_review':
        if current_actor(request).role != 'student': raise AccessDenied('forbidden',403)
        return redirect('/journeys/?template=exchange')
    if request.method == 'GET' and (set(request.GET) - {'type'} or len(request.GET.getlist('type')) > 1 or request_type not in REQUEST_TYPES):
        if current_actor(request).role != 'student':
            raise AccessDenied('forbidden', 403)
        return public_error(request, 400, 'Выберите тип заявки из каталога.')
    if request.method == 'POST':
        try:
            fields = {key: request.POST[key] for names in PAYLOAD_FIELDS.values() for key in names if key in request.POST}
            result = submit_request(request, request.POST.get('context', ''),
                request.POST.get('submission_token', ''), request.POST.get('request_type', ''),
                request.POST.get('comment', ''), fields=fields)
            return saved(request, result, f"/requests/{result['id']}/")
        except RequestError as exc:
            error = exc
    data = snapshot_for_form(request, 'request_new')
    selected = request_type if request_type in REQUEST_TYPES else REQUEST_TYPE
    form = RequestForm(request.POST if request.method == 'POST' else None, request_type=selected)
    data.update(form=form, request_type=selected, type_label=REQUEST_TYPES[selected],
                request_catalog=[{'value': key, 'label': label} for key, label in REQUEST_TYPES.items() if key in PAYLOAD_FIELDS])
    if request.method == 'POST':
        data['submission_token'] = request.POST.get('submission_token', '')
    if error:
        form.is_valid()
        for field, message in error.field_errors.items():
            target = field if field in form.fields else None
            if str(message) not in [str(item) for item in form.errors.get(target or '__all__', [])]:
                form.add_error(target, message)
        for name in form.fields:
            if form.errors.get(name):
                form.fields[name].widget.attrs['aria-describedby'] = ('comment-help ' if name == 'comment' else '') + f'id_{name}_error'
        if error.status == 409:
            data['conflict_message'] = 'Эта отправка уже сохранена с другими данными. Проверьте список заявок. Для нового обращения откройте новую форму.'
        data['error_summary'] = error_summary(form)
    data['component_data'] = request_data(data)
    if request.headers.get('X-Component') == '1' and request.method == 'POST':
        response = JsonResponse(data['component_data'],status=error.status if error else 200)
        response['X-Page-Context'] = data['marker']
        return response
    return page(request, 'request_new.html', data, error.status if error else 200)


def error_summary(form, fallback='request-type'):
    return [{'target': form[field].id_for_label if field in form.fields else fallback, 'message': str(message)}
            for field, messages in form.errors.items() for message in messages]


@require_GET
@request_protected
def request_detail(request, request_id):
    return page(request, 'request_detail.html', private_snapshot(request, section='request_detail', request_id=request_id))

@require_GET
@request_protected
def request_state(request,request_id):
    from django.template.loader import render_to_string
    actor=current_actor(request,expected=request.headers.get('X-Page-Context',''))
    if actor.role not in ('student','staff'): raise AccessDenied('forbidden',403)
    data=private_snapshot(request,section='staff_request_detail' if actor.role=='staff' else 'request_detail',request_id=request_id)
    for action in data.get('actions',[]):
        form=DecisionForm(auto_id='id_'+action['action']+'_%s')
        form.fields['explanation'].label=action['explanation_label']
        field_id=form['explanation'].id_for_label
        form.fields['explanation'].widget.attrs['aria-describedby']=field_id+'_help'
        action.update(form=form,expanded=False)
    html=render_to_string('request_detail.html',data,request=request)
    current_actor(request,expected=actor.marker)
    return JsonResponse({'marker':actor.marker,'version':data['request_item']['version'],'html':html,'viewed':data.get('viewed_request'),
        'clarification':data['clarification_data'],'clarification_viewed':data.get('viewed_events')})


def status_filter(request, default='pending'):
    if set(request.GET) - {'status', 'page'} or len(request.GET.getlist('status')) > 1:
        raise ValueError('filter')
    value = request.GET.get('status', default)
    if value not in ('all', 'pending', *STATUS_LABELS):
        raise ValueError('filter')
    return value


def page_number(request):
    value = request.GET.get('page', '1')
    if len(request.GET.getlist('page')) > 1 or not re.fullmatch(r'[1-9][0-9]{0,8}', value):
        raise ValueError('page')
    return int(value)


@require_GET
@request_protected
def staff_queue(request):
    if current_actor(request).role != 'staff':
        raise AccessDenied('forbidden', 403)
    try:
        allowed={'q','type','group','clarification','status','page'}
        if set(request.GET)-allowed or any(len(request.GET.getlist(k))!=1 for k in request.GET):raise ValueError('filter')
        value = request.GET.get('status','pending')
        if value not in ('all','pending',*STATUS_LABELS):raise ValueError('filter')
        number = page_number(request)
    except ValueError:
        return public_error(request, 400, 'Проверьте статус и номер страницы очереди заявок.')
    return page(request, 'requests.html', private_snapshot(request, section='staff_requests', request_filter=value,
                                                          request_page_number=number,request_queue_filters=request.GET.dict()))


@require_http_methods(['GET', 'POST'])
@sensitive_post_parameters('explanation', 'context')
@request_protected
def staff_detail(request, request_id):
    error = None
    attempted_action = request.POST.get('action', '') if request.method == 'POST' else ''
    if request.method == 'POST':
        try:
            result = transition_request(request, request_id, request.POST.get('context', ''), attempted_action,
                request.POST.get('expected_version', ''), request.POST.get('explanation', ''))
            return saved(request, result, f'/staff/requests/{request_id}/')
        except RequestError as exc:
            error = exc
    data = snapshot_for_form(request, 'staff_request_detail', request_id=request_id)
    if (error and error.status != 409 and 'expected_version' not in error.field_errors
            and request.POST.get('expected_version') != str(data['request_item']['version'])):
        # Другой сотрудник мог commit между откатом validation и свежим GET snapshot.
        # Старое объяснение никогда не связывается автоматически с новой версией.
        error = RequestError('version_conflict', status=409)
    # После конфликта формы не подставляют новую версию к прежнему решению.
    if error and error.status == 409:
        text = ('Это действие недоступно в текущем статусе. Проверьте сохранённое состояние и выберите доступное действие.'
                if error.code == 'transition_conflict' else 'Завершение недоступно: ожидается ответ студента на уточнение.' if error.code == 'clarification_pending' else
                'Заявка уже изменилась. Ниже — сохранённое состояние. Проверьте его перед следующим действием.')
        data.update(conflict_message=text, actions=[])
    for action in data['actions']:
        bound = error and error.status != 409 and action['action'] == attempted_action
        form = DecisionForm(request.POST if bound else None, auto_id='id_' + action['action'] + '_%s')
        form.fields['explanation'].label = action['explanation_label']
        field_id = form['explanation'].id_for_label
        form.fields['explanation'].widget.attrs['aria-describedby'] = field_id + '_help'
        if bound:
            form.is_valid()
            for field, message in error.field_errors.items():
                form.add_error(field if field in form.fields and action['requires_explanation'] else None, message)
            if form.errors.get('explanation'):
                form.fields['explanation'].widget.attrs['aria-describedby'] += ' ' + field_id + '_error'
            data['error_summary'] = error_summary(form, fallback='decision-title')
        action.update(form=form, expanded=bool(bound))
    return page(request, 'request_detail.html', data, error.status if error else 200)
