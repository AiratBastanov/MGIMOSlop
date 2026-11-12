"""Explicit synthetic examples through normal role services; no GET/startup hooks."""
import json
import uuid
from datetime import timedelta
from pathlib import Path
from django.contrib.sessions.backends.db import SessionStore
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import RequestFactory
from django.utils import timezone
from accounts.access import authenticate_session,current_actor,lock_guard,logout_session,identity_for
from accounts.models import Identity
from materials.selectors import assignment_scope,current_policy
from materials.models import Material
from materials.services import save_draft,submit_material,start_correction,review_revision
from student_requests.models import Request
from student_requests.services import issue_submission,submit_request
from student_requests.workflow import REQUEST_TYPE
from .models import WorkspaceOperation,TeachingPublication,Clarification
from .common import record_operation
from .teaching_services import perform
from .faculty_services import save_notice,clarify

NAMESPACE=uuid.UUID('72907582-df03-4d65-82ed-fb916194cb03')
def key(name):return str(uuid.uuid5(NAMESPACE,name))
FINGERPRINT='task16-demo-v1'


def provision(credentials_path,*,operator,edition='legacy'):
    if not operator.strip() or len(operator)>100:raise ValueError('Укажите оператора до 100 символов.')
    if edition not in ('legacy','review-demo-v1'):raise ValueError('Неизвестная композиция демонстрации.')
    namespace=NAMESPACE if edition=='legacy' else uuid.UUID('a2a6d0bd-574f-429d-864b-9bf7dddf272c')
    def key(name):return str(uuid.uuid5(namespace,name))
    fingerprint=FINGERPRINT if edition=='legacy' else 'review-demo-v1'
    label='Демо TASK16' if edition=='legacy' else 'Демо'
    note='TASK16' if edition=='legacy' else 'учебного кабинета'
    with transaction.atomic():
        lock_guard()
        teacher=Identity.objects.get(user__username='teacher_demo')
        _,teacher=identity_for(teacher.user_id)
        if teacher.role!='teacher':raise ValueError('Конфликт роли teacher_demo')
        old=WorkspaceOperation.objects.filter(user=teacher.user,key=key('complete')).first()
        if old:
            if old.fingerprint!=fingerprint:raise ValueError('Конфликт версии синтетического набора')
            return {'state':'noop','operator':operator,**old.result}
        credentials=json.loads(Path(credentials_path).read_text(encoding='utf-8'))
        requests={}
        for name,role in [('teacher_demo','teacher'),('student_a','student'),('staff_demo','staff')]:
            req=RequestFactory().post('/explicit-task16-seed/')
            req.session=SessionStore()
            if authenticate_session(req,name,credentials[name])!=role:raise ValueError('Конфликт учётной записи '+name)
            requests[role]=req
        tr,sr,fr=requests['teacher'],requests['student'],requests['staff']
        ta,sa,fa=(current_actor(requests[r]) for r in ('teacher','student','staff'))
        shared=assignment_scope(ta,ta.checked_at).filter(pk__in=assignment_scope(sa,sa.checked_at).values('pk')).order_by('pk').first()
        if not shared:raise ValueError('Нет действующего общего учебного назначения; данные не изменены.')
        def publication(name,kind='resource',action='publish'):
            data={'kind':kind,'assignment':shared.pk,'title':f'[{label}] '+name,
                'topic':'Синтетический пример, созданный явной командой','body':'Разберите аргумент и приведите два пояснения. Это демонстрация рабочего процесса.',
                'action':action,'operation':key(name),'expected_version':0,'attachment_ids':'[]',
                'modes':['text'],'attachment_types':['txt','pdf','png','jpeg','docx','webp'],'assessment_mode':'numeric',
                'due_at':(timezone.now()+timedelta(days=3)).isoformat()}
            upload=RequestFactory().post('/explicit-task16-seed/',{'files':[
                SimpleUploadedFile('памятка.txt',f'Вымышленная учебная памятка {note}.'.encode(),content_type='text/plain'),
                SimpleUploadedFile('пример.txt','Разберите пример и сформулируйте вывод.'.encode(),content_type='text/plain')]})
            upload.session=tr.session
            result=perform(upload,ta.marker,data)
            return TeachingPublication.objects.get(pk=result['id'])
        resource=publication('Материал с двумя файлами')
        draft=publication('Частный план следующего занятия',action='save')
        pending=publication('Аргумент и два примера',kind='task')
        corrected=publication('Исправление учебного ответа',kind='task')
        response_ids=[]
        for pub in (pending,corrected):
            result=save_draft(sr,sa.marker,{'assignment':shared.pk,'task':pub.task.pk,'title':pub.draft['title'],
                'mode':'text','content':'Синтетический первый ответ для демонстрации.'})
            row=Material.objects.get(pk=result['id'])
            def submit(text):
                row.refresh_from_db()
                submit_material(sr,sa.marker,row.pk,row.draft_version,current_policy(row.assignment_id,row.task_id).version,
                    {'title':row.draft_title,'mode':'text','content':text,'expected_task_revision':pub.published_revision_id})
            submit('Синтетический первый ответ для демонстрации.')
            if pub==corrected:
                review_revision(tr,ta.marker,row.revisions.get().pk,{'status':'changes_requested','feedback':'Синтетический отзыв: уточните вывод.','result':''})
                row.refresh_from_db();start_correction(sr,sa.marker,row.pk,row.draft_version)
                submit('Синтетический исправленный ответ с пояснением вывода.')
            response_ids.append(row.pk)
        notice=save_notice(fr,fa.marker,{'action':'publish','title':f'[{label}] Консультация факультетской службы',
            'body':'Синтетическое объявление: вопросы по демонстрационным заявкам можно уточнить в кабинете.',
            'group':str(shared.group_id),'visible_until':(timezone.now()+timedelta(days=30)).isoformat(),'operation':key('notice')})
        request=submit_request(sr,sa.marker,issue_submission(sa),REQUEST_TYPE,
            f'Синтетический пример {note}: уточнение назначения документа, создано явной командой.')
        row=Request.objects.get(pk=request['id'])
        clarify(fr,fa.marker,row.pk,{'action':'ask','body':'Синтетический вопрос: для какой цели нужен документ?',
            'request_version':row.version,'operation':key('question')})
        exchange=Clarification.objects.get(request=row)
        clarify(sr,sa.marker,row.pk,{'action':'reply','body':'Синтетический ответ: для учебной демонстрации.',
            'clarification':exchange.pk,'expected_version':exchange.version,'operation':key('reply')})
        result={'resource':resource.pk,'private_draft':draft.pk,'tasks':[pending.pk,corrected.pk],'responses':response_ids,
            'notice':notice['id'],'request':row.pk,'created_at':timezone.now().isoformat()}
        record_operation(ta,key('complete'),fingerprint,result)
        for req in requests.values():logout_session(req,current_actor(req).marker)
        return {'state':'created','operator':operator,**result}
