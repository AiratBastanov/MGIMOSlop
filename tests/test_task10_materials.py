"""TASK10: настоящее сохранение, scope преподавателя и неизменные версии."""
import io
import json
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction,IntegrityError
from django.db.models.deletion import ProtectedError
from django.test import Client,TransactionTestCase
from accounts.access import AccessDenied,current_actor,identity_for
from accounts.models import Identity,SessionContext,StaffScope
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply
from data_import.state import canonical_state
from studies.models import Instructor,StudentProfile
from materials.models import TeachingAssignment,SubmissionPolicy,Material,MaterialRevision,MaterialReview
from materials.selectors import current_policy,material_values,workspace
from materials.services import (MaterialError,save_draft,submit_material,start_correction,
    review_revision,update_policy,set_assignment_active)
from tests import test_auth


class MaterialTests(TransactionTestCase):
    login=test_auth.AuthTests.login
    request=test_auth.AuthTests.request
    page=test_auth.AuthTests.page
    post=test_auth.AuthTests.post
    marker=test_auth.AuthTests.marker

    def setUp(self):
        test_auth.AuthTests.setUp(self)
        for revision in range(5,10):
            stage=3 if revision<8 else 4
            batch=SyntheticDatasetProvider().load(test_auth.ROOT/f'fixtures/s{stage}/r{revision}.json')
            apply(batch,expected_revision=revision-1,operator='task10-materials-test')
        call_command('provision_task10',credentials=str(self.credentials_path),operator='task10-test',stdout=io.StringIO())
        self.credentials=json.loads(self.credentials_path.read_text(encoding='utf-8'))
        self.teacher=Client(enforce_csrf_checks=True)
        self.login(self.teacher,'teacher_demo'); self.login()
        self.assignment=TeachingAssignment.objects.filter(group__source_id='G-MB21').order_by('pk').first()

    def create(self,content='Сохранённый материал',mode='text'):
        req=self.request(); actor=current_actor(req)
        result=save_draft(req,actor.marker,{'assignment':str(self.assignment.pk),
            'title':'Учебный материал (демо)','mode':mode,'content':content})
        return Material.objects.get(pk=result['id'])

    def submit(self,row,policy=None):
        row.refresh_from_db(); req=self.request(); actor=current_actor(req)
        return submit_material(req,actor.marker,row.pk,row.draft_version,
            policy or current_policy(row.assignment_id).version)

    def review(self,revision,status='changes_requested',feedback='Добавьте пояснение к примеру.'):
        req=self.request(self.teacher); actor=current_actor(req)
        return review_revision(req,actor.marker,revision.pk,{'status':status,'feedback':feedback})

    def policy(self,**values):
        req=self.request(self.teacher); actor=current_actor(req)
        old=current_policy(self.assignment.pk)
        data={'expected_version':str(old.version),'modes':['text'],'cloud_hosts':[],
            'text_limit':1000,'instructions':'Нужен краткий текст.'}
        data.update(values)
        return update_policy(req,actor.marker,self.assignment.pk,data)

    def test_http_teacher_student_cycle_and_correction_keep_official_facts(self):
        official=canonical_state()
        self.assertEqual(self.teacher.get('/teacher/').status_code,200)
        self.assertEqual(self.teacher.get('/staff/').status_code,403)
        self.assertEqual(self.teacher.get('/study/').status_code,403)
        self.assertEqual(self.a.get('/teacher/').status_code,403)
        marker=current_actor(self.request(self.teacher)).marker
        old=current_policy(self.assignment.pk)
        result=self.post(f'/teacher/assignments/{self.assignment.pk}/',self.teacher,context=marker,
            expected_version=old.version,modes=['text'],text_limit=1000,cloud_hosts=[],instructions='Поясните пример.')
        self.assertEqual(result.status_code,302)
        marker=current_actor(self.request()).marker
        result=self.post('/study/materials/new/',context=marker,assignment=self.assignment.pk,
            title='Новая работа',mode='text',content='Первый текст')
        self.assertEqual(result.status_code,302)
        row=Material.objects.get()
        self.assertEqual(self.a.get(result.url+'editor/').json()['material']['content'],'Первый текст')
        self.assertEqual(self.teacher.get(f'/teacher/materials/{row.pk}/').status_code,404)
        response=self.post(f'/study/materials/{row.pk}/',context=marker,action='submit',
            expected_version=row.draft_version,expected_policy=current_policy(row.assignment_id).version,
            title=row.draft_title,mode=row.draft_mode,content=row.draft_content)
        self.assertEqual(response.status_code,302)
        revision=row.revisions.get(number=1)
        response=self.post(f'/teacher/materials/{row.pk}/',self.teacher,context=current_actor(self.request(self.teacher)).marker,
            revision=revision.pk,status='changes_requested',feedback='Уточните вывод.')
        self.assertEqual(response.status_code,302)
        self.assertEqual(self.a.get(f'/study/materials/{row.pk}/editor/').json()['material']['revisions'][0]['review']['feedback'],'Уточните вывод.')
        row.refresh_from_db()
        start_correction(self.request(),marker,row.pk,row.draft_version)
        row.refresh_from_db()
        save_draft(self.request(),marker,{'expected_version':row.draft_version,'title':'Исправление','mode':'text','content':'Новый вывод'},row.pk)
        self.submit(row)
        self.assertEqual(list(row.revisions.order_by('number').values_list('content',flat=True)),['Первый текст','Новый вывод'])
        self.assertEqual(MaterialReview.objects.get(revision=revision).feedback,'Уточните вывод.')
        self.assertEqual(canonical_state(),official)

    def test_foreign_student_unassigned_teacher_and_staff_never_read_drafts(self):
        row=self.create('Секретный черновик A')
        self.login(self.b,'student_b')
        self.assertEqual(self.b.get(f'/study/materials/{row.pk}/').status_code,404)
        self.assertEqual(self.teacher.get(f'/teacher/materials/{row.pk}/').status_code,404)
        self.submit(row)
        other_instructor=Instructor.objects.exclude(pk=self.assignment.teacher.instructor_id).first()
        user=get_user_model().objects.create_user(username='unassigned_teacher',password='test-teacher-password-only')
        Identity.objects.create(user=user,role='teacher',instructor=other_instructor)
        self.credentials[user.username]='test-teacher-password-only'
        unassigned=Client(enforce_csrf_checks=True); self.login(unassigned,user.username)
        self.assertEqual(unassigned.get(f'/teacher/materials/{row.pk}/').status_code,404)
        revision=row.revisions.get()
        req=self.request(unassigned); actor=current_actor(req)
        with self.assertRaises(AccessDenied):
            review_revision(req,actor.marker,revision.pk,{'status':'accepted','feedback':'Чужой отзыв'})
        staff=Client(enforce_csrf_checks=True); self.login(staff,'staff_demo')
        self.assertEqual(staff.get(f'/teacher/materials/{row.pk}/').status_code,403)
        req=self.request(self.b); actor=current_actor(req)
        with self.assertRaises(AccessDenied): save_draft(req,actor.marker,{'expected_version':1,'title':'Чужая правка','mode':'text','content':'x'},row.pk)
        self.assertFalse(MaterialReview.objects.exists())

    def test_allowed_modes_cloud_host_validation_and_policy_change(self):
        for invalid in ['http://drive.google.com/file','https://drive.google.com.evil.example/a',
                'https://user:pass@drive.google.com/a','https://drive.google.com:444/a','javascript:alert(1)',
                'https://127.0.0.1/a','https://evil.example/drive.google.com']:
            row=self.create(invalid,'link')
            with self.assertRaises(MaterialError): self.submit(row)
            self.assertEqual(row.revisions.count(),0)
        valid=self.create('https://drive.google.com/file/d/synthetic-demo','link')
        self.submit(valid)
        email=self.create('demo.student@example.invalid','email'); self.submit(email)
        bad_email=self.create('bad address','email')
        with self.assertRaises(MaterialError): self.submit(bad_email)
        draft=self.create('https://disk.yandex.ru/d/synthetic-demo','link')
        previous=current_policy(self.assignment.pk).version
        self.policy(modes=['text'],cloud_hosts=[])
        with self.assertRaises(MaterialError) as caught: self.submit(draft,previous)
        self.assertEqual(caught.exception.status,409)
        with self.assertRaises(MaterialError): self.submit(draft)
        self.assertEqual(valid.revisions.get().policy.version,previous)
        self.assertEqual(Material.objects.get(pk=draft.pk).draft_content,'https://disk.yandex.ru/d/synthetic-demo')

    def test_assignment_revocation_invalidates_old_context_and_current_teacher_access(self):
        row=self.create(); self.submit(row)
        teacher_request=self.request(self.teacher); old=current_actor(teacher_request).marker
        self.assertTrue(set_assignment_active(self.assignment.pk,False,operator='task10-test'))
        with self.assertRaises(AccessDenied) as caught: current_actor(teacher_request,expected=old)
        self.assertEqual(caught.exception.status,409)
        self.assertEqual(self.teacher.get(f'/teacher/materials/{row.pk}/').status_code,404)
        self.assertEqual(self.a.get(f'/study/materials/{row.pk}/editor/').json()['material']['content'],'Сохранённый материал')
        actor=current_actor(self.request())
        self.assertFalse(material_values(actor,row.pk,actor.checked_at)['can_submit'])
        self.assertTrue(set_assignment_active(self.assignment.pk,True,operator='task10-test'))
        self.assertEqual(self.teacher.get(f'/teacher/materials/{row.pk}/').status_code,200)

    def test_submitted_revisions_feedback_policies_and_owner_are_database_immutable(self):
        row=self.create(); self.submit(row); revision=row.revisions.get(); self.review(revision)
        actions=[lambda:MaterialRevision.objects.filter(pk=revision.pk).update(content='Подмена'),
            lambda:MaterialReview.objects.filter(revision=revision).update(feedback='Подмена'),
            lambda:SubmissionPolicy.objects.filter(pk=revision.policy_id).update(instructions='Подмена'),
            lambda:Material.objects.filter(pk=row.pk).update(owner=StudentProfile.objects.get(source_id='SP-B')),
            lambda:MaterialRevision.objects.filter(pk=revision.pk).delete()]
        for action in actions:
            with self.subTest(action=str(action)):
                with self.assertRaises((IntegrityError,ProtectedError)):
                    with transaction.atomic(): action()
        self.assertEqual(MaterialRevision.objects.get(pk=revision.pk).content,'Сохранённый материал')

    def test_submit_review_retries_and_stale_draft_conflict(self):
        row=self.create(); req=self.request(); actor=current_actor(req); expected=row.draft_version
        policy=current_policy(row.assignment_id).version
        self.assertTrue(submit_material(req,actor.marker,row.pk,expected,policy)['created'])
        self.assertFalse(submit_material(req,actor.marker,row.pk,expected,policy)['created'])
        revision=row.revisions.get(); self.review(revision); self.review(revision)
        self.assertEqual(MaterialReview.objects.filter(revision=revision).count(),1)
        with self.assertRaises(MaterialError) as caught: self.review(revision,feedback='Другой отзыв')
        self.assertEqual(caught.exception.status,409)
        row.refresh_from_db(); start_correction(req,actor.marker,row.pk,row.draft_version); row.refresh_from_db()
        before=row.draft_version
        save_draft(req,actor.marker,{'expected_version':before,'title':'Новая','mode':'text','content':'Вторая редакция'},row.pk)
        with self.assertRaises(MaterialError) as caught:
            save_draft(req,actor.marker,{'expected_version':before,'title':'Старая','mode':'text','content':'Перезапись'},row.pk)
        self.assertEqual(caught.exception.status,409)
        self.assertEqual(Material.objects.get(pk=row.pk).draft_content,'Вторая редакция')

    def test_validation_keeps_own_input_and_stale_session_does_not_echo_it(self):
        old=current_actor(self.request()).marker
        # TASK11: React сохраняет input в текущем контексте; сервер возвращает field error,
        # не перепечатывает форму. Сохранность видимого ввода проверяется в browser evidence.
        response=self.post('/study/materials/editor/',context=old,action='save',assignment=self.assignment.pk,title='',mode='text',content='Личный ввод при ошибке')
        self.assertEqual(response.status_code,400)
        self.assertEqual(response.json()['marker'],old)
        self.assertEqual(response.json()['error']['field'],'title')
        self.assertEqual(Material.objects.count(),0)
        self.post('/logout/',context=old); self.login(self.a,'student_b')
        response=self.post('/study/materials/editor/',context=old,action='save',assignment=self.assignment.pk,title='Старый текст',mode='text',content='Личный ввод при ошибке')
        self.assertEqual(response.status_code,409); self.assertNotContains(response,'Личный ввод при ошибке',status_code=409)
        self.assertEqual(Material.objects.count(),0)

    def test_teacher_identity_rejects_conflicting_staff_scope_and_admin_flags(self):
        teacher=Identity.objects.get(user__username='teacher_demo')
        staff_scope=StaffScope.objects.get(identity__user__username='staff_demo')
        StaffScope.objects.create(identity=teacher,faculty=staff_scope.faculty)
        with self.assertRaises(AccessDenied): identity_for(teacher.user_id)
        StaffScope.objects.filter(identity=teacher).delete()
        get_user_model().objects.filter(pk=teacher.user_id).update(is_superuser=True)
        with self.assertRaises(AccessDenied): identity_for(teacher.user_id)

    def test_provisioning_is_idempotent_and_draft_survives_new_login(self):
        row=self.create('Черновик после нового входа'); self.policy(text_limit=1500)
        hashes=list(get_user_model().objects.order_by('pk').values_list('pk','password'))
        credentials=self.credentials_path.read_bytes(); policies=SubmissionPolicy.objects.count()
        sessions=list(SessionContext.objects.order_by('pk').values())
        call_command('provision_task10',credentials=str(self.credentials_path),operator='task10-test',stdout=io.StringIO())
        self.assertEqual(self.credentials_path.read_bytes(),credentials)
        self.assertEqual(list(get_user_model().objects.order_by('pk').values_list('pk','password')),hashes)
        self.assertEqual(SubmissionPolicy.objects.count(),policies)
        self.assertEqual(list(SessionContext.objects.order_by('pk').values()),sessions)
        self.post('/logout/',context=current_actor(self.request()).marker); self.login()
        self.assertEqual(self.a.get(f'/study/materials/{row.pk}/editor/').json()['material']['content'],'Черновик после нового входа')
