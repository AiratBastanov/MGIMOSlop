"""TASK10: реальные import границы и срок видимости teaching permissions."""
from datetime import timedelta
import io
import hashlib
import json
from unittest.mock import patch
from PIL import Image
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import Client,TransactionTestCase
from django.utils import timezone
from accounts.access import AccessDenied,current_actor,lock_guard
from accounts.models import Identity,SessionContext,StaffScope
from data_import.services import apply
from data_import.state import records
from materials.models import Material,MaterialRevision,MaterialReview,TeachingAssignment,SubmissionPolicy
from notifications.models import Notification,NotificationRead,NotificationReceipt,EmailOutbox,InboxClear
from notifications.actions import mark_read
from notifications.delivery import sync_recipient
from notifications.inbox_actions import clear_inbox
from notifications.selectors import inbox_events,inbox_page
from profiles.models import Preferences,Avatar,GroupResource,GroupResourceRevision
from profiles.services import save_resource
from student_requests.models import Request,RequestEvent
from student_requests.services import submit_request,issue_submission
from studies.models import StudentProfile,Instructor
from tests import test_auth
from tests import test_task10_materials
from tests.s4_fixtures import signed,change,create


class Task10IntegrityTests(TransactionTestCase):
    # Reuse setup/helpers only: inherited test methods would duplicate old evidence.
    setUp=test_task10_materials.MaterialTests.setUp
    login=test_auth.AuthTests.login
    request=test_auth.AuthTests.request
    page=test_auth.AuthTests.page
    post=test_auth.AuthTests.post
    marker=test_auth.AuthTests.marker
    create=test_task10_materials.MaterialTests.create
    submit=test_task10_materials.MaterialTests.submit
    review=test_task10_materials.MaterialTests.review

    def followup(self,operations):
        payload=json.loads((test_auth.ROOT/'fixtures/s4/r9.json').read_text(encoding='utf-8'))
        payload.update(dataset_revision=10,parent_revision=9,source_as_of=timezone.now().isoformat(),
            prepared_by='Изолированная проверка TASK10',preparation_reason='Проверка сохранности application-owned state',operations=operations)
        return signed(payload)

    def test_new_import_preserves_application_state_and_creates_receipt_outbox_once(self):
        material=self.create('Текст до импорта'); self.submit(material); self.review(material.revisions.get())
        req=self.request(); actor=current_actor(req)
        Preferences.objects.create(user_id=actor.user_id,contact_email='test@example.invalid',email_enabled=True,
            email_verified_at=timezone.now(),photo_audience='context')
        image=io.BytesIO(); Image.new('RGB',(64,64),(30,60,90)).save(image,format='JPEG')
        Avatar.objects.create(identity=actor.identity,image=image.getvalue(),digest=hashlib.sha256(image.getvalue()).hexdigest())
        result=submit_request(req,actor.marker,issue_submission(actor),'study_certificate','Сохранить исходную заявку')
        event=Notification.objects.get(request_event__request_id=result['id'])
        mark_read(req,event.pk,actor.marker)
        clear_inbox(req,actor.marker,inbox_page(actor,now=actor.checked_at)['clear_token'])
        staff=Client(enforce_csrf_checks=True); self.login(staff,'staff_demo')
        staff_request=self.request(staff); staff_actor=current_actor(staff_request)
        save_resource(staff_request,staff_actor.marker,{'version':0,'group':actor.identity.profile.group,
            'category':'document','title':'Синтетический ресурс группы','description':'Сохранить версию ресурса',
            'url':'https://example.invalid/group','available_from':timezone.now()-timedelta(days=1),
            'available_until':timezone.now()+timedelta(days=1)})
        protected=[get_user_model(),Identity,StaffScope,SessionContext,Material,MaterialRevision,MaterialReview,
            TeachingAssignment,SubmissionPolicy,Preferences,Avatar,GroupResource,GroupResourceRevision,Request,RequestEvent,
            NotificationRead,InboxClear]
        before={model._meta.label:list(model.objects.order_by('pk').values()) for model in protected}
        receipts=list(NotificationReceipt.objects.order_by('pk').values())
        outbox=list(EmailOutbox.objects.order_by('pk').values())
        old=records()['final_result']['FR-A-ECON']; source=timezone.now().isoformat()
        batch=self.followup([change('final_result',old,source,'EV-T10-PRESERVE',grade=4,recorded_at=source)])
        apply(batch,expected_revision=9,operator='task10-integrity-test')
        for model in protected:
            self.assertEqual(list(model.objects.order_by('pk').values()),before[model._meta.label],model._meta.label)
        self.assertEqual(list(NotificationReceipt.objects.filter(pk__in=[row['id'] for row in receipts]).order_by('pk').values()),receipts)
        self.assertEqual(list(EmailOutbox.objects.filter(pk__in=[row['id'] for row in outbox]).order_by('pk').values()),outbox)
        added=Notification.objects.get(final_result_change__event_id='EV-T10-PRESERVE')
        self.assertEqual(NotificationReceipt.objects.filter(notification=added,user_id=actor.user_id).count(),1)
        self.assertEqual(EmailOutbox.objects.filter(notification=added,user_id=actor.user_id).count(),1)
        counts=(NotificationReceipt.objects.count(),EmailOutbox.objects.count())
        self.assertEqual(apply(batch,expected_revision=10,operator='task10-integrity-test')['outcome'],'noop')
        sync_recipient(actor.user_id)
        self.assertEqual((NotificationReceipt.objects.count(),EmailOutbox.objects.count()),counts)

    def test_identity_provisioning_registers_future_publication_without_get_writes(self):
        source=timezone.now().isoformat(); future=timezone.now()+timedelta(minutes=10)
        operations=[
            create('profile',source,'SP-T10-FUTURE',name='Новый студент (демо)',group_id='G-MB21',year=2),
            create('announcement',source,'AN-T10-FUTURE',title='Будущая публикация (демо)',body='Синтетическое объявление',
                audience='group',faculty_id=None,group_id='G-MB21',student_id=None,published_at=future.isoformat(),
                visible_until=(future+timedelta(days=1)).isoformat(),due_at=None,status='published')]
        apply(self.followup(operations),expected_revision=9,operator='task10-integrity-test')
        notification=Notification.objects.get(announcement_change__announcement__source_id='AN-T10-FUTURE')
        with transaction.atomic():
            lock_guard()
            user=get_user_model().objects.create_user(username='future_student',password='isolated-future-password')
            Identity.objects.create(user=user,role='student',profile=StudentProfile.objects.get(source_id='SP-T10-FUTURE'))
        receipt=NotificationReceipt.objects.get(user=user,notification=notification)
        self.assertEqual(receipt.available_at,future)
        self.credentials[user.username]='isolated-future-password'
        client=Client(enforce_csrf_checks=True); self.login(client,user.username)
        actor=current_actor(self.request(client))
        before=list(NotificationReceipt.objects.filter(user=user).order_by('pk').values())
        self.assertFalse(inbox_events(actor,now=timezone.now()).filter(pk=notification.pk).exists())
        self.assertTrue(inbox_events(actor,now=future).filter(pk=notification.pk).exists())
        response=client.get('/notifications/')
        self.assertEqual(response.status_code,200)
        self.assertEqual(list(NotificationReceipt.objects.filter(user=user).order_by('pk').values()),before)
        sync_recipient(user.pk); sync_recipient(user.pk)
        self.assertEqual(NotificationReceipt.objects.filter(user=user,notification=notification).count(),1)

    def test_assignment_exact_end_changes_marker_and_privacy_deadline_without_revoking_session(self):
        now=timezone.now(); boundary=now+timedelta(minutes=1)
        instructor=Instructor.objects.exclude(pk=self.assignment.teacher.instructor_id).first()
        user=get_user_model().objects.create_user(username='temporal_teacher',password='isolated-boundary-password')
        identity=Identity.objects.create(user=user,role='teacher',instructor=instructor)
        TeachingAssignment.objects.create(teacher=identity,course=self.assignment.course,group=self.assignment.group,
            period=self.assignment.period,available_from=now-timedelta(minutes=1),available_until=boundary)
        self.credentials[user.username]='isolated-boundary-password'
        client=Client(enforce_csrf_checks=True); self.login(client,user.username)
        req=self.request(client); old=current_actor(req)
        student_request=self.request(); old_student=current_actor(student_request)
        self.assertEqual(old.expires_at,boundary); self.assertEqual(old_student.expires_at,boundary)
        context_before=SessionContext.objects.get(pk=old.context_id)
        self.assertGreater(context_before.expires_at,boundary)
        with patch('accounts.access.timezone.now',return_value=boundary):
            with self.assertRaises(AccessDenied) as caught: current_actor(req,expected=old.marker)
            self.assertEqual(caught.exception.status,409)
            with self.assertRaises(AccessDenied): current_actor(student_request,expected=old_student.marker)
            fresh=current_actor(req)
            self.assertNotEqual(fresh.marker,old.marker)
            self.assertEqual(fresh.expires_at,context_before.expires_at)
            self.assertEqual(client.get('/teacher/').status_code,200)
        context_after=SessionContext.objects.get(pk=old.context_id)
        self.assertEqual(context_after.expires_at,context_before.expires_at)
        self.assertIsNone(context_after.revoked_at)
