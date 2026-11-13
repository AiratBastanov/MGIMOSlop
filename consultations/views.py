from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST

from accounts.access import AccessDenied, current_actor
from accounts.components import component_response, enhanced
from accounts.selectors import private_snapshot
from accounts.views import protected
from materials.services import MaterialError
from .selectors import page_data
from .services import perform


def _snapshot(request, booking_id=None):
    if any(len(values) != 1 for _, values in request.GET.lists()):
        raise AccessDenied('invalid_query', 400)
    def extension(actor, now):
        data = page_data(actor, now, request.GET.dict(), booking_id)
        result = {'section': {'student': 'requests', 'teacher': 'teacher_courses', 'staff': 'staff'}[actor.role],
                  'consultation_data': data}
        if booking_id:
            from workspaces.views import event_view
            result['viewed_events'] = event_view(actor, now, application_event__consultation_event__booking_id=booking_id)
        return result
    return private_snapshot(request, extension=extension)


@require_GET
@protected
def page(request, booking_id=None):
    data = _snapshot(request, booking_id)
    response = render(request, 'consultations/page.html', data)
    response['X-Page-Context'] = data['marker']
    return response


@require_GET
@protected
def state(request, booking_id=None):
    return component_response(request, _snapshot(request, booking_id)['consultation_data'])


@require_POST
@sensitive_post_parameters('question', 'context')
@protected
def action(request):
    try:
        result = perform(request, request.POST.get('context', ''), request.POST)
        current_actor(request, expected=result['marker'])
        response = JsonResponse(result)
    except MaterialError as exc:
        actor = current_actor(request, expected=request.POST.get('context', ''))
        response = JsonResponse({'marker': actor.marker, 'state': 'invalid', 'message': exc.message,
                                 'field': exc.field or ''}, status=exc.status)
    response['X-Page-Context'] = current_actor(request).marker
    return response
