"""TASK11 F01/F02: текущий ввод, атомарность, повторы и узкий scoped API."""
from unittest.mock import patch
from django import forms
from django.db import DatabaseError
from django.test import TransactionTestCase
from accounts.access import current_actor
from data_import.state import canonical_state
from materials.forms import DraftForm
from materials.models import Material,MaterialRevision,MaterialReview
from materials.selectors import current_policy
from materials.services import save_draft,start_correction
from tests import test_task10_materials as previous


class CurrentMaterialTests(TransactionTestCase):
    # Проверенные fixture helpers, без наследования/повторного запуска исторического suite.
    setUp=previous.MaterialTests.setUp
    login=previous.MaterialTests.login
    request=previous.MaterialTests.request
    page=previous.MaterialTests.page
    post=previous.MaterialTests.post
    marker=previous.MaterialTests.marker
    create=previous.MaterialTests.create
    review=previous.MaterialTests.review
    policy=previous.MaterialTests.policy

    def send(self,row,**changes):
        payload={'context':current_actor(self.request()).marker,'action':'submit',
            'expected_version':row.draft_version,'expected_policy':current_policy(row.assignment_id).version,
            'title':'Текущая работа B','mode':'text','content':'Текст B из открытого редактора'}
        payload.update(changes)
        return self.post(f'/study/materials/{row.pk}/editor/',**payload)

    def test_current_visible_payload_is_saved_and_submitted_atomically(self):
        row=self.create('A — прежний черновик')
        before=canonical_state()
        response=self.send(row)
        self.assertEqual(response.status_code,200,response.content)
        result=response.json()
        self.assertEqual(result['state'],'submitted'); self.assertTrue(result['created'])
        row.refresh_from_db(); revision=row.revisions.get()
        self.assertEqual((row.draft_title,row.draft_mode,row.draft_content),
            ('Текущая работа B','text','Текст B из открытого редактора'))
        self.assertEqual((revision.title,revision.content),(row.draft_title,row.draft_content))
        self.assertFalse(row.has_draft)
        self.assertContains(self.teacher.get(f'/teacher/materials/{row.pk}/'),'Текст B из открытого редактора')
        self.assertEqual(canonical_state(),before)
        self.assertEqual(result['marker'],response['X-Page-Context'])
        self.assertIn('no-store',response['Cache-Control'])

    def test_missing_current_payload_cannot_silently_send_saved_draft(self):
        row=self.create('A — не отправлять автоматически')
        response=self.post(f'/study/materials/{row.pk}/',context=current_actor(self.request()).marker,
            action='submit',expected_version=row.draft_version,expected_policy=current_policy(row.assignment_id).version)
        self.assertEqual(response.status_code,400)
        self.assertFalse(row.revisions.exists())
        self.assertEqual(Material.objects.get(pk=row.pk).draft_content,'A — не отправлять автоматически')

    def test_revision_failure_rolls_back_the_preliminary_payload_save(self):
        row=self.create('A')
        with patch('materials.services.MaterialRevision.objects.create',side_effect=DatabaseError('simulated insert failure')):
            response=self.send(row)
        self.assertEqual(response.status_code,503)
        row.refresh_from_db()
        self.assertEqual((row.draft_content,row.draft_version,row.submission_count),('A',1,0))
        self.assertTrue(row.has_draft); self.assertFalse(row.revisions.exists())

    def test_lost_committed_response_and_exact_retry_do_not_duplicate_revision(self):
        row=self.create('A')
        policy=current_policy(row.assignment_id).version
        with patch('materials.views.editor_response',side_effect=DatabaseError('simulated response failure')):
            response=self.send(row,expected_policy=policy)
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json(),{'state':'save_unconfirmed'})
        self.assertEqual(row.revisions.count(),1)
        # Условия изменились после commit: точный повтор старой операции остаётся replay.
        self.policy(text_limit=800,instructions='Уточнённые условия следующей отправки.')
        response=self.send(row,expected_policy=policy)
        self.assertEqual(response.status_code,200,response.content)
        self.assertFalse(response.json()['created'])
        self.assertEqual(row.revisions.count(),1)
        conflicting=self.send(row,expected_policy=policy,content='C — другой payload с прежним ключом')
        self.assertEqual(conflicting.status_code,409)
        self.assertEqual(row.revisions.get().content,'Текст B из открытого редактора')

    def test_policy_conflict_and_validation_do_not_partially_save_or_submit(self):
        row=self.create('A')
        old=current_policy(row.assignment_id).version
        self.policy(modes=['text'],text_limit=100)
        conflict=self.send(row,expected_policy=old)
        self.assertEqual(conflict.status_code,409)
        self.assertEqual(conflict.json()['state'],'conflict')
        self.assertEqual(conflict.json()['assignments'][0]['policy']['version'],old+1)
        for fields in ({'mode':'email','content':'student@example.invalid'}, {'content':'B'*101}, {'content':''}):
            response=self.send(row,**fields)
            self.assertEqual(response.status_code,400,response.content)
            row.refresh_from_db()
            self.assertEqual((row.draft_content,row.draft_version,row.submission_count),('A',1,0))
            self.assertFalse(row.revisions.exists())
        accepted=self.send(row)
        self.assertEqual(accepted.status_code,200)
        self.assertEqual(row.revisions.get().policy.version,old+1)

    def test_concurrent_draft_edit_is_a_conflict_and_keeps_current_saved_text(self):
        row=self.create('A')
        marker=current_actor(self.request()).marker
        save_draft(self.request(),marker,{'expected_version':row.draft_version,
            'title':'Сохранено в другой вкладке','mode':'text','content':'C'},row.pk)
        response=self.send(row)
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json()['material']['content'],'C')
        self.assertEqual(response.json()['material']['version'],2)
        self.assertFalse(row.revisions.exists())

    def test_save_retry_and_correction_preserve_previous_revision_and_feedback(self):
        row=self.create('A')
        marker=current_actor(self.request()).marker
        save={'context':marker,'action':'save','expected_version':row.draft_version,
            'title':'Черновик B','mode':'text','content':'B'}
        first=self.post(f'/study/materials/{row.pk}/editor/',**save)
        repeat=self.post(f'/study/materials/{row.pk}/editor/',**save)
        self.assertEqual(first.status_code,200); self.assertEqual(repeat.status_code,200)
        self.assertFalse(repeat.json()['created'])
        row.refresh_from_db(); self.assertEqual(row.draft_version,2)
        self.assertEqual(self.send(row).status_code,200)
        first_revision=row.revisions.get(); self.review(first_revision)
        row.refresh_from_db()
        response=self.post(f'/study/materials/{row.pk}/editor/',context=marker,action='correct',expected_version=row.draft_version)
        self.assertEqual(response.status_code,200)
        row.refresh_from_db()
        self.assertEqual(self.send(row,content='Вторая отправленная версия').status_code,200)
        self.assertEqual(list(row.revisions.order_by('number').values_list('content',flat=True)),
            ['Текст B из открытого редактора','Вторая отправленная версия'])
        self.assertEqual(MaterialReview.objects.get(revision=first_revision).feedback,'Добавьте пояснение к примеру.')

    def test_editor_api_is_scoped_csrf_protected_and_does_not_echo_stale_context(self):
        row=self.create('Приватный черновик A')
        path=f'/study/materials/{row.pk}/editor/'
        marker=current_actor(self.request()).marker
        self.assertEqual(self.a.post(path,{'context':marker,'action':'submit'}).status_code,403)
        self.login(self.b,'student_b')
        self.assertEqual(self.b.get(path).status_code,404)
        self.assertEqual(self.teacher.get(path).status_code,403)
        response=self.a.get(path,HTTP_X_PAGE_CONTEXT='outdated')
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json(),{'state':'stale_context'})
        self.post('/logout/',context=marker); self.login(self.a,'student_b')
        response=self.post(path,context=marker,action='submit',expected_version=1,expected_policy=1,
            title='Старый ввод A',mode='text',content='Не раскрывать в B')
        self.assertEqual(response.status_code,409)
        self.assertEqual(response.json(),{'state':'stale_context'})
        self.assertFalse(row.revisions.exists())

    def test_assignment_modes_and_form_widgets_follow_current_policy(self):
        self.policy(modes=['link'],cloud_hosts=['disk.yandex.ru'],instructions='Передайте ссылку на конспект.')
        response=self.a.get('/study/materials/editor/')
        self.assertEqual(response.status_code,200)
        assignment=next(row for row in response.json()['assignments'] if row['id']==self.assignment.pk)
        self.assertEqual(assignment['policy']['modes'],['link'])
        self.assertTrue(assignment['policy']['allow_link']); self.assertFalse(assignment['policy']['allow_text'])
        self.assertIn('instructor_person',assignment)
        policy=assignment['policy']
        form=DraftForm(initial={'mode':'link'},editing=True,policy=policy)
        self.assertEqual(list(form.fields['mode'].choices),[('link','Ссылка на облако')])
        self.assertIsInstance(form.fields['content'].widget,forms.URLInput)
        self.assertIn('disk.yandex.ru',form.fields['content'].help_text)
        row=self.create('A')
        self.assertEqual(self.send(row,mode='link',content='https://evil.example/').status_code,400)
        self.assertEqual(self.send(row,mode='link',content='https://disk.yandex.ru/d/synthetic').status_code,200)
        self.assertEqual(row.revisions.get().mode,'link')
        email_policy={**policy,'modes':['email']}
        form=DraftForm(initial={'mode':'email'},editing=True,policy=email_policy)
        self.assertIsInstance(form.fields['content'].widget,forms.EmailInput)

    def test_new_editor_creates_only_owned_draft_and_ignores_forged_assignment_on_edit(self):
        marker=current_actor(self.request()).marker
        response=self.post('/study/materials/editor/',context=marker,action='save',assignment=self.assignment.pk,
            title='Новая работа',mode='text',content='Первый черновик',owner='SP-B',role='teacher')
        self.assertEqual(response.status_code,200,response.content)
        item=response.json()['material']; row=Material.objects.get(pk=item['id'])
        self.assertEqual(row.owner.source_id,'SP-A'); self.assertFalse(row.revisions.exists())
        response=self.post(f'/study/materials/{row.pk}/editor/',context=marker,action='save',assignment=99999,
            expected_version=row.draft_version,title='Вторая редакция',mode='text',content='Текст')
        self.assertEqual(response.status_code,200)
        row.refresh_from_db(); self.assertEqual(row.assignment_id,self.assignment.pk)
