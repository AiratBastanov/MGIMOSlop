"""Narrow presentation adapters. Facts, counts and pagination remain in selectors."""
from django.http import JsonResponse
from django.template.loader import render_to_string
from django.utils.formats import date_format
from .access import current_actor

def enhanced(request):
    return request.headers.get('X-Component') == '1'

def component_response(request, data):
    current_actor(request, expected=request.headers.get('X-Page-Context', ''))
    response = JsonResponse(data)
    response['X-Page-Context'] = data['marker']
    return response

def schedule_data(request, data):
    selected = data['selected_date']
    # Selected day's facts always precede the rest of the week on small screens.
    days = sorted(data['days'], key=lambda day: (day['date'] != selected, day['date']))
    return {'marker': data['marker'], 'selected': data['selected_iso'], 'view': data['view'],
        'group':data['group'], 'today':data['today_date'].isoformat(),
        'period':date_format(data['range_start'], 'j E') + (' — ' + date_format(data['range_end'], 'j E Y') if data['view'] == 'week' else ' ' + str(selected.year)),
        'previous':data['previous_url'], 'next':data['next_url'],
        'days':[{'date':d['date_iso'],'state':d['state']} for d in data['month_days']],
        'html':render_to_string('partials/schedule_agenda.html', {**data,'days':days}, request=request),
        'source':render_to_string('partials/source_info.html', data, request=request),
        'error':data.get('date_error','')}

def inbox_data(request, data):
    return {'marker':data['marker'], 'kind':data['notification_kind'], 'archive':data['archive_mode'], 'mode':data['inbox_mode'],
        'count':data['notification_count'], 'unread':data['unread'],
        'html':render_to_string('partials/inbox.html', data, request=request),
        'clear_token':data.get('clear_token',''),
        'read_all_token':data['read_all_token'],
        'undo':str(data['undo_batch'].id) if data.get('undo_batch') else None}

def request_data(data):
    from student_requests.forms import RequestForm
    def fields(form):
        rows = []
        for name, field in form.fields.items():
            item = {'name':name,'label':field.label,'required':field.required,
                'type':'textarea' if field.widget.__class__.__name__ == 'Textarea' else 'text',
                'value':form[name].value() or '', 'errors':list(form.errors.get(name, []))}
            for attr in ('max_length','min_value','max_value'):
                if getattr(field,attr,None) is not None:
                    item[attr] = getattr(field,attr)
            if hasattr(field,'choices'):
                item.update(type='select',choices=list(field.choices))
            elif hasattr(field,'min_value'):
                item['type'] = 'number'
            rows.append(item)
        return rows
    return {'marker':data['marker'], 'token':data['submission_token'], 'selected':data['request_type'],
        'types':[{**item,'fields':fields(data['form'] if item['value']==data['request_type'] else RequestForm(request_type=item['value']))} for item in data['request_catalog']],
        'name':data['name'], 'group':data['group'],'faculty':data['faculty'], 'programme':data['programme'],
        'year':data['year'], 'person':data['person'], 'conflict':data.get('conflict_message',''),
        'errors':list(data['form'].non_field_errors())}
