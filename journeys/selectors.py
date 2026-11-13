from django.utils import timezone
from accounts.access import AccessDenied
from studies.academic_selectors import academic_snapshot
from studies.selectors import schedule_snapshot, moscow
from student_requests.selectors import request_values
from workspaces.models import Clarification
from .models import Journey
from .templates import TEMPLATES, DOCUMENTS, NEEDS, PROGRAM


def journey_scope(actor):
    if actor.role != 'student': raise AccessDenied('forbidden',403)
    return Journey.objects.filter(owner_id=actor.profile_id)


def record_options(actor, now):
    academic=academic_snapshot(actor,now)
    schedule=schedule_snapshot(actor,now)
    options=[{'key':'profile','label':'Учебные сведения моего профиля','area':'profile','url':'/profile/','revision':actor.identity.profile.scope_revision}]
    if academic['source']['state']=='ready':
        periods={p['id']:p['name'] for p in academic['periods']}
        for row in academic['courses'][:150]:
            options.append({'key':f'enrollment:{row["id"]}','label':row['course_name']+' · '+periods.get(row['period_id'],''),
                'area':'result','url':f'/study/enrollments/{row["id"]}/','revision':row.get('record_revision',0)})
    if schedule['source']['state']=='ready':
        for row in schedule['lessons'][:150]:
            options.append({'key':f'lesson:{row["id"]}','label':row['course_name']+' · '+row['date'].strftime('%d.%m.%Y')+' · '+row['time_label'],
                'area':'schedule','url':row['url'],'revision':row['record_revision']})
    messages=[s['source']['message'] for s in (academic,schedule) if s['source']['state']!='ready']
    return options, messages


def summary(row):
    return {'id':row.pk,'title':TEMPLATES[(row.template_key,row.template_revision)]['title'],
        'template_key':row.template_key,'template_revision':row.template_revision,'version':row.version,
        'url':f'/journeys/{row.pk}/','updated_at':moscow(row.updated_at).isoformat()}


def journey_values(actor,row,now):
    template=TEMPLATES[(row.template_key,row.template_revision)]
    submissions=[]
    for link in row.submissions.select_related('request','revision').order_by('-created_at','-pk')[:20]:
        request=request_values(link.request)
        pending=Clarification.objects.filter(request=link.request,state='open').exists()
        submissions.append({'id':link.pk,'version':link.revision.version,'request_id':link.request_id,
            'reference':request['reference'],'url':request['url'],'status':request['status'],'status_label':request['status_label'],
            'needs_reply':pending,'newer_edits':row.version>link.revision.version,
            'result':link.request.events.order_by('-version').first().explanation})
    latest=submissions[0] if submissions else None
    status=('Нужны уточнения' if latest and latest['needs_reply'] else
            'Ожидается ответ' if latest and latest['status'] in ('submitted','in_progress') else
            'Следующие действия получены' if latest and latest['status']=='ready' else
            'Обращение отклонено · проверьте причину' if latest and latest['status']=='rejected' else 'Подготовка')
    from consultations.selectors import booking_scope, booking_values
    links=[]
    own=booking_scope(actor,now)
    for link in row.booking_links.select_related('booking','booking__slot').order_by('-pk')[:20]:
        booking=own.filter(pk=link.booking_id).first()
        if booking: links.append(booking_values(actor,booking,now,history=False))
    options,messages=record_options(actor,now) if row.template_key=='correction' else ([],[])
    available_bookings=[booking_values(actor,b,now,history=False) for b in own.order_by('-pk')[:20]]
    return {**summary(row),'state':row.state,'template':template,'status_label':status,
        'program':PROGRAM if row.template_key=='exchange' else '',
        'documents':[{'key':key,'label':label} for key,label in DOCUMENTS.items()],
        'needs':list(NEEDS.items()),'submissions':submissions,'bookings':links,'available_bookings':available_bookings,
        'records':options,'source_messages':messages}


def page_data(actor,now,journey_id=None):
    query=journey_scope(actor)
    if journey_id is None:
        existing={r.template_key:r for r in query.order_by('pk')}
        return {'marker':actor.marker,'templates':[{'key':key,'revision':revision,**template,
            'existing':summary(existing[key]) if key in existing else None} for (key,revision),template in TEMPLATES.items()]}
    row=query.filter(pk=journey_id).first()
    if not row: raise AccessDenied('not_found',404)
    return {'marker':actor.marker,'journey':journey_values(actor,row,now)}
