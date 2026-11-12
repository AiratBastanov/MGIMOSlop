"""Focused TASK14 data ownership, repeatability and scoped presentation checks."""
import json
from io import StringIO
from pathlib import Path
from django.core.management import call_command
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from accounts.access import current_actor
from accounts.models import Identity, AccessAudit, SessionContext
from data_import.demo_task14 import prepare_results, REFERENCE
from data_import.models import Dataset
from data_import.services import apply, preview, check_integrity, get_head
from data_import.state import records
from notifications.models import Notification, NotificationReceipt, EmailOutbox
from profiles.demo_task14 import provision_contacts, ACTION, simulated_owner
from profiles.models import Preferences
from profiles.selectors import group_directory
from studies.academic_selectors import academic_snapshot, study_view
from studies.models import FinalResult
from tests import test_task10_materials

class Task14Tests(TransactionTestCase):
    login=test_task10_materials.MaterialTests.login
    request=test_task10_materials.MaterialTests.request
    page=test_task10_materials.MaterialTests.page
    post=test_task10_materials.MaterialTests.post
    marker=test_task10_materials.MaterialTests.marker
    def setUp(self):
        test_task10_materials.MaterialTests.setUp(self)
        call_command('provision_task11_directory',operator='task14-test',expected_revision=9,
            output=str(Path(self.temp.name)/'directory.json'),apply=True,stdout=StringIO())
        self.capture_root=Path(self.temp.name)
        (self.capture_root/'.local/task14-protected').mkdir(parents=True)

    def test_results_reference_scope_history_and_exact_replay(self):
        before=records(4);counts=(Notification.objects.count(),NotificationReceipt.objects.count())
        batch=prepare_results(10);self.assertEqual(preview(batch,expected_revision=10)['outcome'],'preview')
        self.assertEqual(records(4),before)
        apply(batch,expected_revision=10,operator='task14-test')
        after=records(4)
        for kind,rows in before.items():
            for key,row in rows.items():self.assertEqual(after[kind][key],row)
        reference=json.loads(REFERENCE.read_text(encoding='utf-8'))
        actual=list(FinalResult.objects.filter(enrollment__student__source_id='SP-A'))
        self.assertEqual(len(actual),22)
        for row in reference['results']:
            result=next(r for r in actual if r.source_id=='FR-'+row['id'])
            self.assertEqual(result.grade if result.result_kind=='grade' else result.pass_status,row['value'])
            self.assertEqual(result.enrollment.course.name,row['course'])
            self.assertLessEqual(result.recorded_at,timezone.now())
        actor=current_actor(self.request());snapshot=academic_snapshot(actor,actor.checked_at)
        self.assertEqual(len(study_view(snapshot,period_id='all',filter_name='assessed')['courses']),22)
        self.assertEqual(len(study_view(snapshot,period_id='all',filter_name='unentered')['courses']),1)
        for period,n in [('P-T14-2025-autumn',8),('P-T14-2026-spring',8),('P-2026-autumn',6)]:
            self.assertEqual(len(study_view(snapshot,period_id=period,filter_name='assessed')['courses']),n)
        self.login(self.b,'student_b');actor_b=current_actor(self.request(self.b))
        self.assertEqual(len(study_view(academic_snapshot(actor_b,actor_b.checked_at),period_id='all',filter_name='assessed')['courses']),2)
        self.assertEqual(self.b.get('/study/?period=P-T14-2025-autumn').status_code,404)
        self.assertEqual(Notification.objects.count()-counts[0],20)
        now_counts=(Notification.objects.count(),NotificationReceipt.objects.count())
        self.assertEqual(apply(batch,expected_revision=11,operator='task14-test')['outcome'],'noop')
        self.assertIsNone(prepare_results(11))
        self.assertEqual((Notification.objects.count(),NotificationReceipt.objects.count()),now_counts)
        check_integrity(get_head())

    def test_contacts_capture_consent_scope_replay_and_revocation(self):
        before=list(Preferences.objects.order_by('pk').values());outbox=list(EmailOutbox.objects.values())
        self.assertEqual(len(provision_contacts(operator='task14-test')),3)
        self.assertEqual(list(Preferences.objects.order_by('pk').values()),before)
        with override_settings(BASE_DIR=self.capture_root):result=provision_contacts(operator='task14-test',apply=True)
        self.assertTrue(all(r['outcome']=='initialized' for r in result))
        d=Preferences.objects.get(user__username='classmate_daria')
        self.assertTrue(d.email_verified_at);self.assertTrue(d.share_email)
        self.assertTrue(EmailOutbox.objects.filter(user=d.user,status='captured',confirmed_at__isnull=False).exists())
        for row in outbox:self.assertEqual(EmailOutbox.objects.values().get(pk=row['id']),row)
        people=group_directory(current_actor(self.request()))
        self.assertEqual(sum('directory_email' in p for p in people),1)
        self.assertEqual(sum('directory_phone' in p for p in people),2)
        html=self.a.get('/profile/group/').content.decode()
        self.assertNotIn('sofia.lebedeva@example.test',html);self.assertNotIn('+447700900103',html)
        self.assertIn('mailto:daria.volkova@example.test',html)
        self.login(self.b,'student_b');self.assertEqual(group_directory(current_actor(self.request(self.b))),[])
        self.assertNotIn('daria.volkova@example.test',self.b.get('/profile/group/').content.decode())
        # A real service opt-out remains authoritative on repetition.
        identity=Identity.objects.get(user=d.user)
        from profiles.services import update_profile
        from profiles.forms import ProfileForm
        with simulated_owner(identity) as req:
            values={f:getattr(d,f) for f in ProfileForm.Meta.fields};values.update(version=d.version,share_email=False,share_phone=False)
            form=ProfileForm(values,instance=d);self.assertTrue(form.is_valid(),form.errors)
            update_profile(req,current_actor(req).marker,form.cleaned_data)
        with override_settings(BASE_DIR=self.capture_root):again=provision_contacts(operator='task14-test',apply=True)
        self.assertTrue(all(r['outcome']=='noop' for r in again))
        self.assertEqual(AccessAudit.objects.filter(action=ACTION).count(),3)
        self.assertFalse(SessionContext.objects.filter(user__username__startswith='classmate_',revoked_at__isnull=True).exists())
        d.refresh_from_db();self.assertFalse(d.share_email);self.assertFalse(d.share_phone)

    def test_modified_contact_is_preserved_and_protocols_are_safe(self):
        pref=Preferences.objects.get(user__username='classmate_daria')
        pref.version+=1;pref.contact_email='owner@example.test';pref.save()
        with override_settings(BASE_DIR=self.capture_root):result=provision_contacts(operator='task14-test',apply=True)
        self.assertEqual(result[0]['outcome'],'conflict_preserved')
        pref.refresh_from_db();self.assertEqual(pref.contact_email,'owner@example.test')
        pref.share_email=True;pref.email_verified_at=timezone.now();pref.contact_email='bad@example.test\r\nBcc:x@example.test'
        pref.share_phone=True;pref.directory_phone='javascript:alert(1)';pref.save()
        people=group_directory(current_actor(self.request()))
        person=next(p for p in people if p['name']=='Дарья Волкова')
        self.assertNotIn('directory_email',person);self.assertNotIn('directory_phone',person)

    def test_profile_summary_is_owner_scoped_and_role_specific(self):
        response=self.a.get('/profile/');self.assertContains(response,'Не указана')
        self.assertContains(response,'Редактировать профиль');self.assertContains(response,'Контакты и видимость')
        Preferences.objects.update_or_create(user_id=current_actor(self.request()).user_id,defaults={
            'contact_email':'owner-only@example.test','directory_phone':'+447700900199','share_email':False,'share_phone':False})
        self.assertContains(self.a.get('/profile/'),'owner-only@example.test')
        self.login(self.b,'student_b');self.assertNotContains(self.b.get('/profile/'),'owner-only@example.test')
        self.assertNotContains(self.teacher.get('/profile/'),'Контакты и видимость')
        from django.test import Client
        staff=Client(enforce_csrf_checks=True)
        self.login(staff,'staff_demo');self.assertNotContains(staff.get('/profile/'),'Контакты и видимость')
