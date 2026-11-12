from django.shortcuts import redirect
from django.views.decorators.http import require_POST
from accounts.views import protected
from .inbox_actions import clear_inbox, undo_clear, InboxConflict
from django.http import JsonResponse
from accounts.access import current_actor
from accounts.components import enhanced
from . import recipient_actions

@require_POST
@protected
def recipient_action(request, action):
    marker=request.POST.get('context','')
    try:
        if action=='read-all': result=recipient_actions.read_all(request,marker,request.POST.get('snapshot',''))
        elif action=='viewed-request': result=recipient_actions.acknowledge_request(request,marker,request.POST.get('snapshot',''))
        else: result=recipient_actions.trash_event(request,marker,request.POST.get('snapshot',''),restore=action=='restore')
    except InboxConflict as exc:
        return JsonResponse({'state':'conflict','marker':current_actor(request,expected=marker).marker,'error':str(exc)},status=409)
    return JsonResponse({'state':'saved',**result})


@require_POST
@protected
def clear(request):
    batch = clear_inbox(request, request.POST.get('context', ''), request.POST.get('snapshot', ''))
    if enhanced(request):
        return JsonResponse({'state':'saved','marker':current_actor(request,expected=request.POST.get('context','')).marker,'undo':str(batch)})
    return redirect('/notifications/?undo=' + str(batch))


@require_POST
@protected
def undo(request, batch_id):
    try:
        undo_clear(request, request.POST.get('context', ''), batch_id)
    except InboxConflict as exc:
        if enhanced(request):
            return JsonResponse({'state':'conflict','marker':current_actor(request,expected=request.POST.get('context','')).marker,'error':str(exc)},status=409)
        from accounts.views import public_error
        return public_error(request,409,str(exc))
    if enhanced(request):
        return JsonResponse({'state':'saved','marker':current_actor(request,expected=request.POST.get('context','')).marker})
    return redirect('/notifications/')
