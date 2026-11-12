"""Подготовка только добавлений; обычный import владеет записями и событиями."""
import json
from pathlib import Path
from django.conf import settings
from django.utils import timezone
from .contracts import DATASET_ID, content_hash, validate
from .models import ImportRevision, Coverage, AcademicCoverage
from .services import read_snapshot, get_head, check_integrity
from .state import records

REFERENCE = Path(settings.BASE_DIR) / 'fixtures/task14/results-reference.json'

def prepare_results(expected_revision):
    reference=json.loads(REFERENCE.read_text(encoding='utf-8'))
    with read_snapshot():
        head=get_head();check_integrity(head)
        if head.current_revision!=expected_revision:
            raise ValueError('Принятый head изменился; подготовьте новый preview.')
        revision=ImportRevision.objects.get(dataset=head,revision=head.current_revision)
        if revision.schema_version!=4:
            raise ValueError('Нужен принятый source schema4.')
        state=records(4);stamp=timezone.now().isoformat();operations=[]
        def add(kind,row):
            existing=state[kind].get(row['source_id'])
            if existing:
                from datetime import datetime
                def same(key,value):
                    old=existing[key]
                    if key in ('recorded_at','opened_at','due_at','closed_at') and value is not None:
                        return datetime.fromisoformat(old)==datetime.fromisoformat(value)
                    return old==value
                if any(not same(k,v) for k,v in row.items()):
                    raise ValueError('Конфликт стабильного ID: '+row['source_id'])
                return
            value={**row,'record_revision':1,'source_updated_at':stamp}
            op={'operation':'create','entity_type':kind,'record':value}
            if kind in ('enrollment','final_result','requirement'):op['event_id']='EV-'+row['source_id']
            operations.append(op);state[kind][row['source_id']]=value
        for period in reference['periods']:add('period',period)
        for row in reference['results']:
            key=row['id'];enrollment='EN-'+key;result='FR-'+key
            add('course',{'source_id':'C-'+key,'name':row['course'],'period_id':row['period']})
            add('enrollment',{'source_id':enrollment,'student_id':'SP-A','course_id':'C-'+key,'period_id':row['period'],'status':'active'})
            numeric=type(row['value']) is int
            add('final_result',{'source_id':result,'enrollment_id':enrollment,'result_kind':'grade' if numeric else 'pass_fail',
                'grade':row['value'] if numeric else None,'pass_status':None if numeric else row['value'],'recorded_at':row['recorded_at']})
            if row.get('requirement'):
                add('requirement',{'source_id':'REQ-'+key,'enrollment_id':enrollment,'result_id':result,'reason':row['requirement'],
                    'status':'open','opened_at':row['recorded_at'],'due_at':None,'closed_at':None})
        if not operations:return None
        coverage=[{'entity_type':'lesson','group_id':r.group.source_id,'period_id':r.period.source_id,
            'date_from':r.date_from.isoformat(),'date_until':r.date_until.isoformat(),'completeness':r.completeness,
            'active_ids':r.active_ids,'active_count':r.active_count} for r in Coverage.objects.filter(revision=revision).select_related('group','period')]
        academic=[{'entity_type':r.entity_type,'student_id':r.student.source_id,'period_id':r.period.source_id,
            'completeness':r.completeness,'record_ids':r.record_ids,'record_count':r.record_count}
            for r in AcademicCoverage.objects.filter(revision=revision).select_related('student','period')]
        for period in {r['period'] for r in reference['results']}:
            ids={r['source_id'] for r in state['enrollment'].values() if r['student_id']=='SP-A' and r['period_id']==period and r['status']=='active'}
            for kind in ('enrollment','final_result','requirement'):
                values=sorted(ids if kind=='enrollment' else [r['source_id'] for r in state[kind].values() if r['enrollment_id'] in ids])
                old=next((r for r in academic if (r['student_id'],r['period_id'],r['entity_type'])==('SP-A',period,kind)),None)
                if old:
                    old.update(record_ids=values,record_count=len(values))
                else:
                    academic.append({'entity_type':kind,'student_id':'SP-A','period_id':period,'completeness':'complete','record_ids':values,'record_count':len(values)})
        payload={'schema_version':4,'subset':'s4_studies_v4','dataset_id':DATASET_ID,'dataset_revision':head.current_revision+1,
            'parent_revision':head.current_revision,'provenance':'synthetic','prepared_by':'TASK14 local synthetic results',
            'preparation_reason':'18 дополнительных вымышленных результатов student_a, два прошлых периода; существующие записи сохранены.',
            'source_as_of':stamp,'valid_from':revision.valid_from.isoformat(),'valid_until':revision.valid_until.isoformat(),
            'operations':operations,'coverage':coverage,'academic_coverage':academic}
        payload['content_sha256']=content_hash(payload)
        return validate(payload)
