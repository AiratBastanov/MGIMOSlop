"""TASK16: isolated PostgreSQL, real role services and separate-connection races."""
import json
import threading
import time
import uuid
from datetime import timedelta
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, connections, transaction, IntegrityError
from django.http import QueryDict
from django.test import Client, TransactionTestCase
from django.utils import timezone
from accounts.access import current_actor, AccessDenied
from accounts.models import Identity, StaffScope
from accounts.selectors import private_snapshot, domain_notifications
from data_import.state import canonical_state,records
from materials.models import Material, MaterialRevision, MaterialReview
from materials.services import save_draft,submit_material,start_correction,review_revision,MaterialError,set_assignment_active
from materials.selectors import current_policy,material_values
from notifications.models import Notification, NotificationReceipt
from student_requests.models import Request
from student_requests.services import issue_submission,submit_request,transition_request,RequestError
from student_requests.workflow import REQUEST_TYPE
from studies.models import Faculty,Instructor
from workspaces.models import TeachingPublication,TeachingRevision,PublicationFile,ApplicationEvent,StaffNotice,Clarification,ClarificationMessage
from workspaces.teaching_services import perform,task_submission
from workspaces.faculty_services import save_notice,clarify
from workspaces.selectors import publication_values,teaching_scope,notice_scope,review_queue,student_today,teacher_schedule
from tests import test_task10_materials as legacy


class WorkspaceTests(TransactionTestCase):
    setUp=legacy.MaterialTests.setUp
    login=legacy.MaterialTests.login
    request=legacy.MaterialTests.request
    page=legacy.MaterialTests.page
    post=legacy.MaterialTests.post
    marker=legacy.MaterialTests.marker

    def staff(self):
        client=Client(enforce_csrf_checks=True);self.login(client,'staff_demo');return client

    def publish(self,kind='resource',row=None,action='publish',client=None,**extra):
        client=client or self.teacher
        data={'kind':kind,'assignment':self.assignment.pk,'title':'Синтетическая публикация','topic':'Краткие условия',
            'body':'Текущий текст автора','links':'','action':action,'operation':str(uuid.uuid4()),'attachment_ids':'[]',
            'expected_version':row.version if row else 0,'modes':['text'],'attachment_types':['txt','png'],'assessment_mode':'numeric'}
        data.update(extra)
        req=self.request(client)
        result=perform(req,current_actor(req).marker,data,row.pk if row else None)
        return TeachingPublication.objects.get(pk=result['id']),data

    def response(self,pub,client=None,content='Текущий ответ'):
        client=client or self.a;req=self.request(client);actor=current_actor(req)
        result=save_draft(req,actor.marker,{'assignment':pub.assignment_id,'task':pub.task.pk,'title':'Ответ на задание','mode':'text','content':content})
        return Material.objects.get(pk=result['id'])

    def submit(self,row,client=None,**extra):
        row.refresh_from_db();req=self.request(client);actor=current_actor(req)
        data={'title':row.draft_title,'mode':'text','content':row.draft_content,
            'expected_task_revision':row.task.publication.published_revision_id}
        data.update(extra)
        return submit_material(req,actor.marker,row.pk,row.draft_version,current_policy(row.assignment_id,row.task_id).version,data)

    def question(self,staff=None):
        staff=staff or self.staff();sr=self.request();actor=current_actor(sr)
        result=submit_request(sr,actor.marker,issue_submission(actor),REQUEST_TYPE,'Синтетический неизменный запрос')
        row=Request.objects.get(pk=result['id']);req=self.request(staff)
        data={'action':'ask','body':'Уточните назначение документа','request_version':row.version,'operation':str(uuid.uuid4())}
        clarify(req,current_actor(req).marker,row.pk,data)
        return row,Clarification.objects.get(request=row),staff

    def test_resources_draft_current_publish_files_audience_and_withdraw(self):
        before=canonical_state();marker=current_actor(self.request(self.teacher)).marker
        file=SimpleUploadedFile('методика.txt','Синтетический файл'.encode(),content_type='text/plain')
        response=self.post('/teacher/publications/new/editor/',self.teacher,context=marker,kind='resource',
            assignment=self.assignment.pk,title='Приватный текст',body='До публикации',action='save',operation=str(uuid.uuid4()),
            attachment_ids='[]',expected_version=0,files=[file])
        self.assertEqual(response.status_code,200,response.content[:400])
        row=TeachingPublication.objects.get();f=PublicationFile.objects.get()
        self.assertEqual(self.a.get(f'/study/teaching/{row.pk}/').status_code,404)
        self.assertEqual(self.a.get(f'/teaching/files/{f.pk}/').status_code,404)
        self.assertFalse(ApplicationEvent.objects.exists())
        row,data=self.publish(row=row,title='Видимый текст',body='Именно текущий ввод',attachment_ids=json.dumps([str(f.pk)]))
        self.assertContains(self.a.get(f'/study/teaching/{row.pk}/'),'Именно текущий ввод')
        url=f'/teaching/files/{f.pk}/?revision={row.published_revision_id}'
        self.assertEqual(self.a.get(url).status_code,200)
        self.login(self.b,'student_b');self.assertEqual(self.b.get(url).status_code,404)
        other=Instructor.objects.exclude(pk=self.assignment.teacher.instructor_id).first()
        user=get_user_model().objects.create_user('isolated_teacher16',password='isolated-only')
        Identity.objects.create(user=user,role='teacher',instructor=other)
        self.credentials[user.username]='isolated-only';teacher=Client();self.login(teacher,user.username)
        self.assertEqual(teacher.get(url).status_code,404)
        req=self.request(self.teacher);count=TeachingRevision.objects.count()
        perform(req,current_actor(req).marker,data,row.pk)
        self.assertEqual(TeachingRevision.objects.count(),count)
        row,_=self.publish(row=row,action='save',body='Будущий секрет',attachment_ids=json.dumps([str(f.pk)]))
        self.assertNotContains(self.a.get(f'/study/teaching/{row.pk}/'),'Будущий секрет')
        row,_=self.publish(row=row,action='withdraw')
        self.assertEqual(self.a.get(url).status_code,404)
        self.assertEqual(PublicationFile.objects.count(),1)
        self.assertEqual(canonical_state(),before)

    def test_task_submission_review_correction_and_exact_revision(self):
        official=canonical_state();pub,_=self.publish('task');row=self.response(pub)
        teacher=current_actor(self.request(self.teacher))
        roster=publication_values(teacher,pub,teacher.checked_at,detail=True)['roster']
        self.assertEqual(roster[0]['state']['code'],'unsubmitted')
        self.assertEqual(review_queue(teacher,teacher.checked_at,{'state':'pending'})['count'],0)
        with self.assertRaises(MaterialError):self.response(pub)
        self.submit(row,content='Первый видимый ответ')
        rev=row.revisions.get();self.assertEqual(rev.content,'Первый видимый ответ')
        self.assertEqual(rev.task_revision_id,pub.published_revision_id)
        self.assertEqual(rev.submitted_at.isoformat(),rev.task_conditions['accepted_at'])
        req=self.request(self.teacher)
        review_revision(req,current_actor(req).marker,rev.pk,{'status':'changes_requested','feedback':'Уточните вывод','result':''})
        sr=self.request();row.refresh_from_db();start_correction(sr,current_actor(sr).marker,row.pk,row.draft_version)
        self.assertEqual(review_queue(teacher,teacher.checked_at,{'state':'changes_requested'})['count'],1)
        self.submit(row,content='Исправленный видимый ответ')
        newest=row.revisions.order_by('-number').first()
        self.assertFalse(hasattr(newest,'review'))
        self.assertEqual(review_queue(teacher,teacher.checked_at,{'state':'pending'})['count'],1)
        review_revision(req,current_actor(req).marker,newest.pk,{'status':'accepted','feedback':'Принято','result':'5'})
        self.assertEqual(row.revisions.count(),2);self.assertEqual(MaterialReview.objects.count(),2)
        self.assertEqual(canonical_state(),official)
        self.assertTrue(NotificationReceipt.objects.filter(user_id=teacher.user_id,notification__application_event__submission=newest).exists())
        self.assertEqual(self.a.get(f'/study/materials/{row.pk}/editor/').json()['material']['revisions'][0]['content'],'Исправленный видимый ответ')
        with self.assertRaises(MaterialError):self.publish('task',row=pub,assessment_mode='pass_fail')

    def test_deadline_cutoff_revision_conflict_closed_edit_and_retry(self):
        due=timezone.now()+timedelta(minutes=10);cutoff=due+timedelta(minutes=5)
        pub,_=self.publish('task',due_at=due.isoformat(),cutoff_at=cutoff.isoformat())
        row=self.response(pub);actor=current_actor(self.request());rev=pub.published_revision
        self.assertFalse(task_submission(actor,row,rev.pk,now=due)[1]['late'])
        self.assertTrue(task_submission(actor,row,rev.pk,now=due+timedelta(microseconds=1))[1]['late'])
        with self.assertRaises(MaterialError):task_submission(actor,row,rev.pk,now=cutoff)
        with self.assertRaises(MaterialError):task_submission(actor,row,rev.pk+100,now=due)
        with patch('materials.services.timezone.now',return_value=due):self.submit(row)
        # Saved retry succeeds after cutoff, after current authority has been checked.
        row.refresh_from_db();req=self.request();policy=current_policy(row.assignment_id,row.task_id)
        with patch('materials.services.timezone.now',return_value=cutoff+timedelta(hours=1)):
            result=submit_material(req,current_actor(req).marker,row.pk,row.draft_version-2,policy.version,
                {'title':row.draft_title,'mode':'text','content':row.draft_content,'expected_task_revision':rev.pk})
        self.assertFalse(result['created'])
        pub,_=self.publish('task',row=pub,action='close')
        pub,_=self.publish('task',row=pub,action='save',body='Несохранённая в публикации редакция')
        pub,_=self.publish('task',row=pub,action='reopen')
        self.assertEqual(pub.draft['body'],'Несохранённая в публикации редакция')
        self.assertEqual(pub.published_revision.payload['body'],'Текущий текст автора')
        pub,_=self.publish('task',row=pub,action='close')
        pub,_=self.publish('task',row=pub,action='publish')
        self.assertFalse(pub.published_revision.payload['accepting'])

    def test_rollback_missing_notification_conflicts_and_immutable_rows(self):
        with patch('notifications.models.Notification.objects.create',side_effect=RuntimeError('isolated notification failure')):
            with self.assertRaises(RuntimeError):self.publish()
        self.assertEqual(TeachingPublication.objects.count(),0);self.assertEqual(ApplicationEvent.objects.count(),0)
        pub,_=self.publish()
        with self.assertRaises(MaterialError):self.publish(row=pub,expected_version=1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():TeachingRevision.objects.filter(pk=pub.published_revision_id).update(payload={})
        with self.assertRaises(IntegrityError):
            with transaction.atomic():ApplicationEvent.objects.create(kind='teaching',teaching_revision=TeachingRevision.objects.create(
                publication=pub,number=2,payload=pub.draft,files=[],state='published',author=self.assignment.teacher))

    def test_revoked_assignment_does_not_leak_future_task_policy(self):
        pub,_=self.publish('task');row=self.response(pub);self.submit(row)
        pub,_=self.publish('task',row=pub,topic='Новая закрытая инструкция')
        set_assignment_active(self.assignment.pk,False,operator='isolated-task16')
        actor=current_actor(self.request());value=material_values(actor,row.pk,actor.checked_at)
        self.assertIsNone(value['task_revision_id']);self.assertNotIn('Новая закрытая',json.dumps(value,ensure_ascii=False,default=str))
        self.assertFalse(value['can_submit'])
        with self.assertRaises(AccessDenied):self.submit(row)

    def test_notice_current_scope_expiry_and_notifications_for_roles(self):
        staff=self.staff();req=self.request(staff);actor=current_actor(req)
        data={'action':'publish','title':'Объявление службы','body':'Только свой факультет','group':str(self.assignment.group_id),'operation':str(uuid.uuid4()),
            'visible_until':(timezone.now()+timedelta(hours=1)).isoformat()}
        result=save_notice(req,actor.marker,data);notice=StaffNotice.objects.get(pk=result['id'])
        student=current_actor(self.request());self.assertTrue(notice_scope(student,student.checked_at).filter(pk=notice.pk).exists())
        self.assertFalse(notice_scope(student,student.checked_at+timedelta(hours=2)).filter(pk=notice.pk).exists())
        self.assertContains(self.a.get(f'/notices/{notice.pk}/'),'Только свой факультет')
        self.login(self.b,'student_b');self.assertEqual(self.b.get(f'/notices/{notice.pk}/').status_code,404)
        from data_import.contracts import validate
        from data_import.services import apply
        from tests.test_import import signed
        from tests.test_auth import ROOT
        from studies.models import StudentProfile
        current=records();stamp=timezone.now().isoformat()
        batch=json.loads((ROOT/'fixtures/s4/r9.json').read_text(encoding='utf-8'))
        additions=[
            ('faculty',dict(next(iter(current['faculty'].values())),source_id='F-T16-OTHER',name='Вымышленный другой факультет')),
            ('programme',dict(next(iter(current['programme'].values())),source_id='PR-T16-OTHER',faculty_id='F-T16-OTHER')),
            ('group',dict(next(iter(current['group'].values())),source_id='G-T16-OTHER',programme_id='PR-T16-OTHER')),
            ('profile',dict(current['profile']['SP-B'],source_id='SP-T16-OTHER',group_id='G-T16-OTHER',name='Вымышленный участник другого факультета'))]
        batch.update(dataset_revision=10,parent_revision=9,source_as_of=stamp,operations=[
            {'operation':'create','entity_type':kind,'record':dict(value,record_revision=1,source_updated_at=stamp)} for kind,value in additions])
        apply(validate(signed(batch)),expected_revision=9,operator='isolated-task16-faculty')
        foreign=Faculty.objects.get(source_id='F-T16-OTHER')
        other_user=get_user_model().objects.create_user('isolated_student16',password='isolated-only')
        Identity.objects.create(user=other_user,role='student',profile=StudentProfile.objects.get(source_id='SP-T16-OTHER'))
        self.credentials[other_user.username]='isolated-only';other_client=Client();self.login(other_client,other_user.username)
        self.assertEqual(other_client.get(f'/notices/{notice.pk}/').status_code,404)
        user=get_user_model().objects.create_user('isolated_staff16',password='isolated-only')
        identity=Identity.objects.create(user=user,role='staff',display_name='Вымышленный сотрудник другого факультета');StaffScope.objects.create(identity=identity,faculty=foreign)
        self.credentials[user.username]='isolated-only';client=Client();self.login(client,user.username)
        self.assertEqual(client.get(f'/staff/notices/{notice.pk}/').status_code,404)
        self.assertEqual(self.teacher.get('/notifications/').status_code,200)
        self.assertEqual(staff.get('/notifications/').status_code,200)
        data.update(action='withdraw',operation=str(uuid.uuid4()),expected_version=notice.version)
        save_notice(req,actor.marker,data,notice.pk)
        self.assertNotContains(self.a.get(f'/notices/{notice.pk}/'),'Только свой факультет')
        tasks,notices=student_today(student,student.checked_at);self.assertEqual(notices,[])

    def test_clarification_reply_terminal_retry_and_immutable_payload(self):
        row,exchange,staff=self.question();before=(row.payload_data,row.payload_fingerprint,row.version)
        req=self.request(staff);actor=current_actor(req)
        transition_request(req,row.pk,actor.marker,'take',row.version)
        row.refresh_from_db()
        with self.assertRaises(RequestError):transition_request(req,row.pk,actor.marker,'complete',row.version,explanation='Демо-документ готов')
        sr=self.request();student=current_actor(sr)
        data={'action':'reply','body':'Сохранённое дополнение','clarification':exchange.pk,'expected_version':exchange.version,'operation':str(uuid.uuid4())}
        result=clarify(sr,student.marker,row.pk,data)
        clarify(sr,student.marker,row.pk,data)
        self.assertEqual(ClarificationMessage.objects.filter(action='reply').count(),1)
        transition_request(req,row.pk,actor.marker,'complete',row.version,explanation='Демо-документ готов')
        self.assertTrue(clarify(sr,student.marker,row.pk,data)['repeated'])
        row.refresh_from_db();self.assertEqual((row.payload_data,row.payload_fingerprint),before[:2])
        self.assertEqual(row.status,'ready')
        self.assertEqual(self.teacher.get(f'/requests/{row.pk}/').status_code,403)
        data.update(operation=str(uuid.uuid4()),expected_version=result['version'])
        with self.assertRaises(MaterialError):clarify(sr,student.marker,row.pk,data)

    def test_reply_and_reject_two_real_connections_both_orders(self):
        for first in ('reply','reject'):
            row,exchange,staff=self.question()
            sr=self.request();tr=self.request(staff);student=current_actor(sr);actor=current_actor(tr)
            data={'action':'reply','body':'Ответ перед решением','clarification':exchange.pk,'expected_version':exchange.version,'operation':str(uuid.uuid4())}
            held=threading.Event();release=threading.Event();pids={};out={}
            def run(name):
                def barrier(execute,sql,params,many,context):
                    value=execute(sql,params,many,context)
                    if name==first and not held.is_set() and 'accounts_authorizationguard' in sql and 'FOR UPDATE' in sql:
                        held.set()
                        if not release.wait(8):raise RuntimeError('barrier timeout')
                    return value
                try:
                    with connections['default'].cursor() as c:c.execute('SELECT pg_backend_pid()');pids[name]=c.fetchone()[0]
                    with connections['default'].execute_wrapper(barrier):
                        if name=='reply':clarify(sr,student.marker,row.pk,data)
                        else:transition_request(tr,row.pk,actor.marker,'reject',row.version,explanation='Синтетический отказ')
                    out[name]='ok'
                except MaterialError:out[name]='conflict'
                except Exception as exc:out[name]=repr(exc)
                finally:connections['default'].close()
            second='reject' if first=='reply' else 'reply'
            a=threading.Thread(target=run,args=(first,));b=threading.Thread(target=run,args=(second,))
            a.start()
            try:
                self.assertTrue(held.wait(5));b.start();deadline=time.monotonic()+5;blocked=False
                while time.monotonic()<deadline:
                    if second in pids:
                        with connection.cursor() as c:c.execute('SELECT pg_blocking_pids(%s)',[pids[second]]);blocked=pids[first] in c.fetchone()[0]
                        if blocked:break
                    time.sleep(.01)
                self.assertTrue(blocked);self.assertNotEqual(pids[first],pids[second])
            finally:release.set();a.join(8);b.join(8)
            self.assertEqual(out.get('reject'),'ok',out)
            self.assertEqual(out.get('reply'),'ok' if first=='reply' else 'conflict',out)
            self.assertEqual(exchange.messages.filter(action='reply').count(),1 if first=='reply' else 0)
            row.refresh_from_db();self.assertEqual(row.status,'rejected')

    def test_role_routes_scoped_counts_today_and_no_get_writes(self):
        draft,_=self.publish('task',action='save')
        self.assertEqual(self.teacher.get(f'/teacher/publications/{draft.pk}/').status_code,200)
        pub,_=self.publish('task');row=self.response(pub)
        actor=current_actor(self.request())
        self.assertEqual(len(student_today(actor,actor.checked_at)[0]),1)
        self.assertContains(self.a.get('/today/'),'Задание преподавателя')
        staff=self.staff()
        routes=[(self.teacher,p) for p in ('/teacher/','/teacher/courses/','/teacher/schedule/?view=week','/teacher/review/','/notifications/')]
        routes += [(staff,p) for p in ('/staff/','/staff/requests/','/staff/groups/','/staff/notices/','/staff/resources/')]
        before=(TeachingPublication.objects.count(),ApplicationEvent.objects.count(),NotificationReceipt.objects.count())
        for client,path in routes:
            with self.subTest(path=path):self.assertEqual(client.get(path).status_code,200)
        self.assertEqual(before,(TeachingPublication.objects.count(),ApplicationEvent.objects.count(),NotificationReceipt.objects.count()))
        value=teacher_schedule(current_actor(self.request(self.teacher)),timezone.now())
        self.assertNotEqual(value['source']['message'],'Занятий нет')

    def test_explicit_demo_noop_preserves_user_edits(self):
        from workspaces.demo import provision
        result=provision(self.credentials_path,operator='isolated-task16-seed')
        self.assertEqual(result['state'],'created')
        row=TeachingPublication.objects.get(pk=result['resource'])
        self.publish(row=row,action='save',body='Самостоятельное изменение после инициализации')
        before=(TeachingPublication.objects.count(),MaterialRevision.objects.count(),ApplicationEvent.objects.count())
        repeated=provision(self.credentials_path,operator='isolated-repeat')
        self.assertEqual(repeated['state'],'noop')
        row.refresh_from_db();self.assertEqual(row.draft['body'],'Самостоятельное изменение после инициализации')
        self.assertEqual(before,(TeachingPublication.objects.count(),MaterialRevision.objects.count(),ApplicationEvent.objects.count()))

    def test_new_recipient_outbox_atomic_read_all_and_new_revision_unread(self):
        from profiles.models import Preferences
        from notifications.models import EmailOutbox,NotificationRead
        from notifications.inbox_actions import snapshot_token
        from notifications.recipient_actions import read_all
        actor=current_actor(self.request())
        Preferences.objects.update_or_create(user_id=actor.user_id,defaults={'email_enabled':True,'contact_email':'task16@example.invalid',
            'email_verified_at':timezone.now(),'email_academic':True})
        pub,_=self.publish('task')
        event=Notification.objects.get(application_event__teaching_revision=pub.published_revision)
        self.assertTrue(EmailOutbox.objects.filter(user_id=actor.user_id,notification=event).exists())
        req=self.request();read_all(req,actor.marker,snapshot_token(actor,[event.pk],'read-all'))
        self.assertTrue(NotificationRead.objects.filter(user_id=actor.user_id,notification=event).exists())
        pub,_=self.publish('task',row=pub,body='Изменённые инструкции')
        fresh=Notification.objects.get(application_event__teaching_revision=pub.published_revision)
        self.assertFalse(NotificationRead.objects.filter(user_id=actor.user_id,notification=fresh).exists())
        self.assertEqual(EmailOutbox.objects.filter(user_id=actor.user_id,notification=fresh).count(),1)

    def test_staff_queue_search_clarification_and_pagination_keep_filters(self):
        staff=self.staff();row,exchange,_=self.question(staff)
        sr=self.request();actor=current_actor(sr)
        for n in range(11):
            submit_request(sr,actor.marker,issue_submission(actor),REQUEST_TYPE,f'Синтетический дополнительный запрос {n}')
        response=staff.get('/staff/requests/?status=all')
        self.assertEqual(response.status_code,200);self.assertEqual(response.context['request_count'],12)
        self.assertEqual(len(response.context['requests']),10)
        self.assertIn('page=2',response.context['page_next_url'])
        response=staff.get('/staff/requests/?status=all&clarification=open')
        self.assertEqual(response.status_code,200);self.assertEqual(response.context['request_count'],1)
        self.assertEqual(response.context['requests'][0]['id'],row.pk)
        response=staff.get('/staff/requests/?status=all&q='+row.reference)
        self.assertEqual(response.status_code,200);self.assertEqual(response.context['request_count'],1)

    def test_opening_boundary_late_joiner_and_truthful_current_labels(self):
        now=timezone.now();opens=now+timedelta(hours=1)
        pub,_=self.publish('task',opens_at=opens.isoformat())
        row=self.response(pub);actor=current_actor(self.request())
        with self.assertRaises(MaterialError):task_submission(actor,row,pub.published_revision_id,now=opens-timedelta(microseconds=1))
        self.assertFalse(task_submission(actor,row,pub.published_revision_id,now=opens)[1]['late'])
        self.assertEqual(publication_values(actor,pub,now)['revision']['acceptance_label'],'Приём ещё не открыт')
        late,_=self.publish('task',due_at=(now-timedelta(days=1)).isoformat())
        response=self.response(late);conditions=task_submission(actor,response,late.published_revision_id,now=timezone.now())[1]
        self.assertTrue(conditions['late']);self.assertFalse(conditions['deadline_applies'])
        teacher=current_actor(self.request(self.teacher))
        self.assertEqual(publication_values(teacher,late,teacher.checked_at,detail=True)['roster'][0]['state']['label'],'Не отправлено')

    def test_teacher_schedule_replacement_keeps_own_participation_history(self):
        from data_import.contracts import validate
        from data_import.services import apply
        from tests.test_import import signed
        from tests.test_auth import ROOT
        current=records();teacher=current_actor(self.request(self.teacher))
        old=next(v for v in current['lesson'].values() if v['instructor_id']==teacher.identity.instructor.source_id
            and v['course_id']==self.assignment.course.source_id and v['group_id']==self.assignment.group.source_id and v['period_id']==self.assignment.period.source_id)
        stamp=timezone.now().isoformat()
        replacement=dict(next(iter(current['instructor'].values())),source_id='I-T16-REPLACEMENT',record_revision=1,source_updated_at=stamp,name='Вымышленный заменяющий преподаватель')
        after=dict(old,instructor_id=replacement['source_id'],record_revision=old['record_revision']+1,source_updated_at=stamp)
        batch=json.loads((ROOT/'fixtures/s4/r9.json').read_text(encoding='utf-8'))
        batch.update(dataset_revision=10,parent_revision=9,source_as_of=stamp,operations=[
            {'operation':'create','entity_type':'instructor','record':replacement},
            {'operation':'change','entity_type':'lesson','expected_record_revision':old['record_revision'],'event_id':'EV-T16-REPLACEMENT',
                'change_type':'substitute','before':old,'after':after,'reason':'Изолированный тест смены участия'}])
        apply(validate(signed(batch)),expected_revision=9,operator='isolated-task16-schedule')
        schedule=teacher_schedule(teacher,timezone.now())
        self.assertNotIn(old['source_id'],[v['source_id'] for v in schedule['lessons']])
        history=next(h for h in schedule['history'] if h['event_id']=='EV-T16-REPLACEMENT')
        self.assertTrue(history['current']['participation_removed'])
        self.assertEqual(self.teacher.get(history['url']).status_code,200)

    def test_terminal_stale_reply_http_returns_fresh_exchange_without_erasing_input(self):
        row,exchange,staff=self.question()
        req=self.request(staff)
        transition_request(req,row.pk,current_actor(req).marker,'reject',row.version,explanation='Синтетический отказ')
        response=self.post(f'/requests/{row.pk}/clarification/',context=current_actor(self.request()).marker,action='reply',
            body='Ввод до получения нового решения',clarification=exchange.pk,expected_version=1,operation=str(uuid.uuid4()))
        self.assertEqual(response.status_code,409)
        self.assertTrue(response.json()['exchange']['terminal'])
        self.assertEqual(response.json()['exchange']['items'][0]['state'],'resolved')
        self.assertEqual(exchange.messages.filter(action='reply').count(),0)
