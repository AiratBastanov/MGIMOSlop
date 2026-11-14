"""Материализуются только внутри private_snapshot; source validity не относится к заявкам."""
from accounts.access import AccessDenied
from django.core.paginator import Paginator
from django.db.models import Q,Exists,OuterRef
from urllib.parse import urlencode
from accounts.models import StaffScope
from studies.selectors import moscow
from .models import Request
from .workflow import (READY_NOTICES, STATUS_LABELS, TERMINAL, TYPE_LABEL, REQUEST_TYPES,
                       FIELD_LABELS, CERTIFICATE_PURPOSES, CORRECTION_AREAS, available_actions)


def request_scope(actor):
    if actor.role == 'student':
        return Request.objects.filter(owner_id=actor.profile_id)
    if actor.role == 'staff':
        faculty = StaffScope.objects.filter(identity=actor.identity).values('faculty_id')
        return Request.objects.filter(servicing_faculty_id__in=faculty)
    raise AccessDenied('forbidden', 403)


def request_values(row):
    fields = []
    for key, label in FIELD_LABELS.items():
        if key in row.payload_data:
            value = row.payload_data[key]
            if key == 'purpose':
                value = CERTIFICATE_PURPOSES.get(value, value)
            elif key == 'correction_area':
                value = CORRECTION_AREAS.get(value, value)
            fields.append({'label': label, 'value': value})
    if row.request_type == 'exchange_plan_review':
        snapshot=row.payload_data
        fields=[{'label':'Программа','value':snapshot['program']},
            {'label':'Редакция плана','value':snapshot['journey_revision']},
            {'label':'Основание','value':snapshot['source']}]
        for item in snapshot['checks']:
            fields.append({'label':item['label'],'value':'Отмечено студентом' if item['marked_by_student'] else 'Студент ещё не отметил'})
        for item in snapshot['documents']:
            fields.append({'label':item['category'],'value':item['need']+' · '+(item['source'] or 'Источник пока не указан')+' · '+('Отмечено студентом как подготовленное' if item['marked_by_student'] else 'Подготовка не отмечена')})
        fields.extend([{'label':'Вопросы к плану','value':snapshot['questions'] or 'Не добавлены'},
            {'label':'Явно переданная заметка','value':snapshot['shared_note'] or 'Личная заметка не передана'}])
    return {
        'id': row.pk, 'reference': row.reference, 'type': row.request_type,
        'type_label': REQUEST_TYPES.get(row.request_type, TYPE_LABEL),
        'payload_schema': row.payload_schema, 'payload_fields': fields, 'applicant_snapshot': row.applicant_snapshot,
        'status': row.status, 'status_label': STATUS_LABELS[row.status], 'version': row.version,
        'comment': row.comment, 'created_at': moscow(row.created_at), 'updated_at': moscow(row.updated_at),
        'url': f'/requests/{row.pk}/', 'staff_url': f'/staff/requests/{row.pk}/',
        'terminal': row.status in TERMINAL, 'ready_notice': READY_NOTICES[row.request_type] if row.status == 'ready' else '',
        'provenance': row.provenance,
    }


def request_page(actor, request_id=None, status_filter='all', page_number=1, queue_filters=None):
    if status_filter not in ('all', 'pending', *STATUS_LABELS):
        raise ValueError('status_filter')
    query = request_scope(actor).select_related('owner__group', 'servicing_faculty')
    if request_id is not None:
        row = query.filter(pk=request_id).first()
        if row is None:
            raise AccessDenied('not_found', 404)
        item = request_values(row)
        from journeys.models import JourneySubmission
        link=JourneySubmission.objects.select_related('revision').filter(request=row).first()
        if link:
            item['preparation_revision']=link.revision.version
            if actor.role=='student': item['journey_url']=f'/journeys/{link.journey_id}/'
        if actor.role == 'staff':
            item.update(applicant_name=row.applicant_snapshot.get('name', row.owner.name),
                        applicant_group=row.applicant_snapshot.get('group', row.owner.group.name),
                        servicing_faculty=row.servicing_faculty.name)
        events = list(row.events.order_by('version'))
        history = []
        for event in events:
            history.append({'id': event.pk, 'version': event.version, 'action': event.action,
                'from_status': event.from_status, 'to_status': event.to_status,
                'status_label': STATUS_LABELS[event.to_status], 'recorded_at': moscow(event.recorded_at),
                'explanation': event.explanation,
                'actor_label': 'Студент' if event.action == 'submit' else 'Сотрудник',
                'synthetic': bool(event.operator)})
        latest = history[-1]
        item.update(explanation=latest['explanation'],
                    explanation_label='Причина отклонения' if row.status == 'rejected' else 'Результат обработки')
        viewed={}
        if actor.role=='student':
            from notifications.recipient_actions import viewed_token
            viewed={'marker':actor.marker,'id':row.pk,'version':row.version,'token':viewed_token(actor,row)}
        from workspaces.selectors import clarification_values
        from workspaces.views import event_view
        clarification_data={'marker':actor.marker,'request_id':row.pk,'request_version':row.version,'role':actor.role,
            'terminal':item['terminal'],'items':clarification_values(actor,row)}
        return {'request_item': item, 'history': history,
                'viewed_request':viewed,
                'clarification_data':clarification_data,
                'viewed_events':event_view(actor,actor.checked_at,application_event__clarification_message__clarification__request=row),
                'actions': available_actions(row.status) if actor.role == 'staff' else []}
    extra={}
    filters=queue_filters or {}
    if actor.role=='staff':
        from profiles.selectors import publisher_groups
        from workspaces.models import Clarification
        search=filters.get('q','').strip()
        if len(search)>80:raise AccessDenied('invalid_query',400)
        if search:
            predicate=Q(owner__name__icontains=search)|Q(applicant_snapshot__name__icontains=search)
            raw=search.removeprefix('З-').removeprefix('з-')
            if raw.isdigit() and len(raw)<12:predicate|=Q(pk=int(raw))
            query=query.filter(predicate)
        selected_type=filters.get('type','')
        if selected_type:
            if selected_type not in REQUEST_TYPES:raise AccessDenied('invalid_query',400)
            query=query.filter(request_type=selected_type)
        group=filters.get('group','')
        if group:
            if not str(group).isdigit() or not publisher_groups(actor).filter(pk=group).exists():raise AccessDenied('not_found',404)
            query=query.filter(owner__group_id=group)
        clarification=filters.get('clarification','')
        if clarification:
            if clarification not in ('open','answered','resolved'):raise AccessDenied('invalid_query',400)
            query=query.annotate(has_exchange=Exists(Clarification.objects.filter(request_id=OuterRef('pk'),state=clarification))).filter(has_exchange=True)
        extra={'queue_filters':filters,'queue_groups':list(publisher_groups(actor)),'request_types':list(REQUEST_TYPES.items())}
    if status_filter == 'pending':
        query = query.filter(status__in=['submitted', 'in_progress'])
    elif status_filter != 'all':
        query = query.filter(status=status_filter)
    paginator = Paginator(query.order_by('-created_at', '-pk'), 10)
    page = paginator.get_page(page_number)
    rows = []
    for row in page.object_list:
        item = request_values(row)
        from workspaces.models import Clarification
        pending=Clarification.objects.filter(request=row,state__in=['open','answered']).first()
        item['clarification_label']={'open':'Ожидаем уточнение','answered':'Ответ получен'}.get(pending.state,'') if pending else ''
        if actor.role == 'staff':
            item.update(applicant_name=row.applicant_snapshot.get('name', row.owner.name),
                        applicant_group=row.applicant_snapshot.get('group', row.owner.group.name))
        rows.append(item)
    filter_options = [('all', 'Все'), ('pending', 'Без решения'), *STATUS_LABELS.items()]
    base = '/staff/requests/' if actor.role == 'staff' else '/requests/'
    def page_url(number):
        return base + '?' + urlencode({**filters,'status': status_filter, 'page': number})
    return {**extra,'requests': rows, 'request_count': paginator.count, 'filtered_count': paginator.count,
            'page_obj': page, 'page_previous_url': page_url(page.previous_page_number()) if page.has_previous() else '',
            'page_next_url': page_url(page.next_page_number()) if page.has_next() else '',
            'page_notice': ('Такой страницы нет. Показана последняя доступная страница.'
                            if int(page_number) != page.number else ''),
            'status_filter': status_filter,
            'status_filters': [{'value': key, 'label': label} for key, label in filter_options]}
