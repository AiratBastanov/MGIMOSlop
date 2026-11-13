import json
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.debug import sensitive_post_parameters
from accounts.access import current_actor, AccessDenied
from accounts.views import protected
from materials.services import MaterialError
from workspaces.views import snapshot, page
from .selectors import page_data
from .services import perform, preview


@require_GET
@protected
def home(request,journey_id=None):
    data=snapshot(request,'student',lambda actor,now:{'section':'requests','journey_data':page_data(actor,now,journey_id)})
    return page(request,'journeys/page.html',data)


@require_GET
@protected
def state(request,journey_id):
    data=snapshot(request,'student',lambda actor,now:{'journey_data':page_data(actor,now,journey_id)})
    return JsonResponse(data['journey_data'])


@require_POST
@sensitive_post_parameters()
@protected
def action(request,journey_id=None):
    marker=request.headers.get('X-Page-Context','')
    actor=current_actor(request,expected=marker)
    try:
        if len(request.body)>20000: raise MaterialError('Форма слишком велика.')
        try: data=json.loads(request.body)
        except (ValueError,UnicodeDecodeError): raise MaterialError('Форма не распознана.')
        if not isinstance(data,dict): raise MaterialError('Форма не распознана.')
        if data.get('action')=='preview':
            result=preview(request,marker,journey_id,data.get('version'),data.get('share_note',False))
        else: result=perform(request,marker,data,journey_id)
        current_actor(request,expected=result['marker'])
        return JsonResponse(result)
    except MaterialError as exc:
        current_actor(request,expected=marker)
        return JsonResponse({'marker':actor.marker,'state':'invalid','message':str(exc)},status=exc.status)
