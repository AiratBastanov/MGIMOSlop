from django.http import JsonResponse
from django.db import OperationalError
from django.shortcuts import render
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_GET, require_POST
from accounts.access import current_actor
from accounts.components import component_response
from accounts.selectors import private_snapshot
from accounts.views import protected
from .catalog import actions, parse_query, public
from .selectors import search


@require_GET
@protected
def search_page(request):
    data = private_snapshot(request, extension=lambda actor, now: {
        'section': 'search', 'component_data': {'marker': actor.marker,
            'suggestions': [public(item) for item in actions(actor.role)], 'endpoint': '/search/results/'}})
    response = render(request, 'discovery/search.html', data)
    response['X-Page-Context'] = data['marker']
    return response


@require_POST
@sensitive_post_parameters('q', 'context')
@protected
def search_results(request):
    # Query text stays out of URLs and exception reports; this POST is read-only.
    actor = current_actor(request, expected=request.headers.get('X-Page-Context') or request.POST.get('context', ''))
    try:
        if len(request.POST.getlist('q')) != 1 or len(request.POST.getlist('page')) > 1:
            raise ValueError('Укажите один поисковый запрос.')
        query, tokens, page = parse_query(request.POST.get('q', ''), request.POST.get('page', '1'))
    except ValueError as exc:
        return JsonResponse({'marker': actor.marker, 'state': 'invalid', 'error': str(exc)}, status=400)
    try:
        data = private_snapshot(request, extension=lambda active, now: {'search': search(active, now, query, tokens, page)})['search']
    except OperationalError as exc:
        if getattr(exc.__cause__, 'sqlstate', None) != '57014':
            raise
        return JsonResponse({'marker': actor.marker, 'state': 'unavailable',
            'error': 'Поиск не завершён за отведённое время. Уточните запрос или повторите позже.'}, status=503)
    return component_response(request, data)
