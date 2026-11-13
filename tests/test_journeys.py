"""Persistent preparation and immutable deliberate disclosure on isolated PostgreSQL."""
import copy,json,uuid
from datetime import timedelta
from unittest.mock import patch
from django.db import transaction,IntegrityError
from django.test import Client,TransactionTestCase
from django.utils import timezone
from accounts.access import current_actor,AccessDenied
from data_import.state import canonical_state
from materials.services import MaterialError
from notifications.models import Notification
from student_requests.models import Request
from student_requests.services import transition_request
from workspaces.faculty_services import clarify
from journeys.models import Journey,JourneyRevision,JourneySubmission,JourneyOperation,JourneyBooking
from journeys.services import perform,preview
from journeys.selectors import page_data
from tests import test_task10_materials as legacy

class JourneyTests(TransactionTestCase):
    setUp=legacy.MaterialTests.setUp
    login=legacy.MaterialTests.login
    request=legacy.MaterialTests.request
    page=legacy.MaterialTests.page
    post=legacy.MaterialTests.post
    marker=legacy.MaterialTests.marker

    def action(self,data,row=None,client=None):
        req=self.request(client);actor=current_actor(req)
        fields={'operation':str(uuid.uuid4()),**data}
        if row:fields.setdefault('version',row.version)
        return perform(req,actor.marker,fields,row.pk if row else None)
    def start(self,key='exchange'):
        return Journey.objects.get(pk=self.action({'action':'start','template':key})['id'])
    def prepared(self,row):
        state=copy.deepcopy(row.state);state['questions']='Какие дисциплины уточнить?';state['private_note']='НЕПЕРЕДАННАЯ ЗАМЕТКА';state['checks']['requirements' if row.template_key=='exchange' else 'record']=True
        self.action({'action':'save','state':state},row);row.refresh_from_db();return row
    def proof(self,row,share=False):
        req=self.request();return preview(req,current_actor(req).marker,row.pk,row.version,share)
    def send(self,row,share=False):
        result=self.action({'action':'submit','preview_token':self.proof(row,share)['token']},row)
        return Request.objects.get(pk=result['request_id'])
    def staff(self):
        client=Client(enforce_csrf_checks=True);self.login(client,'staff_demo');return client

    def test_start_resume_retry_and_versioned_state_survive_new_session(self):
        official=canonical_state();data={'action':'start','template':'exchange','operation':str(uuid.uuid4())}
        one=self.action(data);two=self.action(data)
        self.assertEqual(one['id'],two['id']);self.assertEqual(Journey.objects.count(),1)
        row=self.prepared(Journey.objects.get(pk=one['id']))
        old_state=copy.deepcopy(row.state)
        saved={'action':'save','version':row.version,'state':old_state,'operation':str(uuid.uuid4())}
        self.action(saved,row)
        state=copy.deepcopy(row.state);state['private_note']='Более новая заметка';self.action({'action':'save','state':state},row);row.refresh_from_db()
        self.action(saved,row)
        self.assertEqual(Journey.objects.get(pk=row.pk).state['private_note'],'Более новая заметка')
        self.post('/logout/',context=current_actor(self.request()).marker)
        self.login()
        page=self.a.get(f'/journeys/{row.pk}/')
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.context['journey_data']['journey']['state']['private_note'],'Более новая заметка')
        self.assertEqual(canonical_state(),official)

    def test_exchange_snapshot_excludes_private_live_note_and_preserves_old_revision(self):
        row=self.prepared(self.start());req=self.send(row);staff=self.staff()
        original=copy.deepcopy(req.payload_data)
        self.assertEqual(req.payload_schema,3);self.assertEqual(original['shared_note'],'')
        self.assertNotContains(staff.get(req.staff_url if hasattr(req,'staff_url') else f'/staff/requests/{req.pk}/'),'НЕПЕРЕДАННАЯ ЗАМЕТКА')
        self.assertContains(staff.get(f'/staff/requests/{req.pk}/'),'Какие дисциплины уточнить?')
        state=copy.deepcopy(row.state);state['questions']='Новая редакция';state['private_note']='НОВЫЙ СЕКРЕТ'
        self.action({'action':'save','state':state},row);row.refresh_from_db();req.refresh_from_db()
        self.assertEqual(req.payload_data,original)
        value=page_data(current_actor(self.request()),timezone.now(),row.pk)['journey']
        self.assertTrue(value['submissions'][0]['newer_edits'])
        self.assertEqual(value['status_label'],'Ожидается ответ')
        self.assertEqual(self.teacher.get(f'/journeys/{row.pk}/').status_code,403)
        self.assertEqual(staff.get(f'/journeys/{row.pk}/').status_code,403)

    def test_actual_clarification_and_rejected_result_are_not_preparation_completion(self):
        row=self.prepared(self.start());req=self.send(row,share=True);self.assertEqual(req.payload_data['shared_note'],'НЕПЕРЕДАННАЯ ЗАМЕТКА')
        staff=self.staff();sr=self.request(staff);actor=current_actor(sr)
        clarify(sr,actor.marker,req.pk,{'action':'ask','body':'Уточните выбранные дисциплины','request_version':req.version,'operation':str(uuid.uuid4())})
        value=page_data(current_actor(self.request()),timezone.now(),row.pk)['journey'];self.assertEqual(value['status_label'],'Нужны уточнения')
        req.refresh_from_db();transition_request(sr,req.pk,actor.marker,'reject',req.version,'Нужно уточнить план; место на обмене не рассматривается.')
        value=page_data(current_actor(self.request()),timezone.now(),row.pk)['journey']
        self.assertIn('отклонено',value['status_label']);self.assertFalse(value['state']['checks']['finish'])

    def test_preview_stale_context_payload_conflict_and_atomic_notification_failure(self):
        row=self.prepared(self.start());proof=self.proof(row)
        state=copy.deepcopy(row.state);state['questions']='Изменённый вопрос';self.action({'action':'save','state':state},row);row.refresh_from_db()
        with self.assertRaises(MaterialError):self.action({'action':'submit','preview_token':proof['token']},row)
        fresh=self.proof(row);data={'action':'submit','preview_token':fresh['token'],'version':row.version,'operation':str(uuid.uuid4())}
        before=Request.objects.count()
        with patch('notifications.services.record_request_event',side_effect=RuntimeError('controlled event fault')):
            with self.assertRaises(RuntimeError):self.action(data,row)
        self.assertEqual(Request.objects.count(),before);self.assertFalse(JourneySubmission.objects.exists())
        first=self.action(data,row);second=self.action(data,row);self.assertEqual(first['request_id'],second['request_id'])
        with self.assertRaises(MaterialError):self.action({**data,'preview_token':'different'},row)

    def test_correction_uses_authorized_record_and_existing_service_without_grade_write(self):
        official=canonical_state();row=self.start('correction')
        value=page_data(current_actor(self.request()),timezone.now(),row.pk)['journey']
        record=next(r for r in value['records'] if r['key'].startswith('enrollment:'))
        state=copy.deepcopy(row.state);state.update(record=record['key'],requested_change='Прошу проверить отображённый результат.',private_note='Секрет')
        self.action({'action':'save','state':state},row);row.refresh_from_db();req=self.send(row)
        self.assertEqual(req.request_type,'academic_correction');self.assertEqual(req.payload_schema,2);self.assertEqual(req.comment,'')
        self.assertEqual(req.payload_data['correction_area'],'result');self.assertEqual(canonical_state(),official)
        state['record']='enrollment:999999';self.action({'action':'save','state':state},row);row.refresh_from_db()
        with self.assertRaises(MaterialError):self.proof(row)

    def test_foreign_ids_malformed_values_csrf_and_no_store(self):
        row=self.start();self.login(self.b,'student_b')
        self.assertEqual(self.b.get(f'/journeys/{row.pk}/').status_code,404)
        self.assertEqual(self.b.get('/journeys/999999/').status_code,404)
        with self.assertRaises(AccessDenied):self.action({'action':'save','state':row.state},row,self.b)
        marker=current_actor(self.request()).marker;url=f'/journeys/{row.pk}/actions/'
        response=self.a.post(url,json.dumps({'action':'link_booking','version':row.version,'booking':[],'operation':str(uuid.uuid4())}),content_type='application/json',HTTP_X_COMPONENT='1',HTTP_X_PAGE_CONTEXT=marker,HTTP_X_CSRFTOKEN=self.a.cookies['csrftoken'].value)
        self.assertEqual(response.status_code,400,response.content)
        state=copy.deepcopy(row.state);state['private_note']='\ud800'
        response=self.a.post(url,json.dumps({'action':'save','version':row.version,'state':state,'operation':str(uuid.uuid4())}),content_type='application/json',HTTP_X_COMPONENT='1',HTTP_X_PAGE_CONTEXT=marker,HTTP_X_CSRFTOKEN=self.a.cookies['csrftoken'].value)
        self.assertEqual(response.status_code,400)
        self.assertEqual(Journey.objects.get(pk=row.pk).version,row.version)
        self.assertEqual(self.a.post(url,'{}',content_type='application/json').status_code,403)
        state=copy.deepcopy(row.state);state['documents']['language']['need']=[]
        with self.assertRaises(MaterialError):self.action({'action':'save','state':state},row)
        with self.assertRaises(MaterialError):self.action({'action':'start','template':{} })
        self.assertIn('no-store',self.a.get(f'/journeys/{row.pk}/')['Cache-Control'])

    def test_database_history_owner_and_revision_are_enforced(self):
        row=self.prepared(self.start());req=self.send(row)
        for mutate in [lambda:Journey.objects.filter(pk=row.pk).update(owner_id=row.owner_id+1),
            lambda:JourneyRevision.objects.filter(journey=row).update(state={}),
            lambda:Request.objects.filter(pk=req.pk).update(payload_data={}),
            lambda:JourneyRevision.objects.create(journey=row,version=row.version+1,state={})]:
            with self.assertRaises(IntegrityError):
                with transaction.atomic():mutate()
        self.assertEqual(JourneyRevision.objects.filter(journey=row).count(),2)
