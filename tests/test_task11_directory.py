"""TASK11: версионное добавление classmates и сохранность существующей работы."""
from hashlib import sha256
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TransactionTestCase

from accounts.access import current_actor
from accounts.models import Identity, SessionContext
from data_import.models import Dataset
from data_import.state import canonical_state
from materials.models import TeachingAssignment
from notifications.models import NotificationRead
from profiles.demo_directory import MEMBERS
from profiles.models import Avatar, GroupResource, Preferences
from profiles.selectors import group_directory
from studies.models import AssessmentEvent, Lesson
from tests import test_task10_materials


class DirectoryProvisionTests(TransactionTestCase):
    setUp = test_task10_materials.MaterialTests.setUp
    login = test_task10_materials.MaterialTests.login
    request = test_task10_materials.MaterialTests.request
    page = test_task10_materials.MaterialTests.page
    post = test_task10_materials.MaterialTests.post
    marker = test_task10_materials.MaterialTests.marker

    def provision(self, *, apply=True, filename='directory.json'):
        command = {'operator': 'isolated-task11-directory',
                   'output': str(Path(self.temp.name) / filename),
                   'expected_revision': Dataset.objects.get().current_revision,
                   'stdout': StringIO()}
        if apply:
            command['apply'] = True
        call_command('provision_task11_directory', **command)

    def test_preview_then_import_adds_only_four_profiles_and_preserves_accounts_history_and_coverage(self):
        before = canonical_state()
        credentials = sha256(self.credentials_path.read_bytes()).hexdigest()
        users = list(get_user_model().objects.order_by('pk').values())
        sessions = list(SessionContext.objects.order_by('pk').values())
        reads = list(NotificationRead.objects.order_by('pk').values())
        self.assertEqual(group_directory(current_actor(self.request())), [])
        self.provision(apply=False, filename='preview.json')
        self.assertEqual(canonical_state(), before)
        with self.assertRaises(CommandError):
            self.provision(filename='preview.json')
        self.assertEqual(canonical_state(), before)
        self.provision()
        after = canonical_state()
        for kind, rows in before['records'].items():
            for source_id, values in rows.items():
                self.assertEqual(after['records'][kind][source_id], values)
        self.assertEqual(set(after['records']['profile']) - set(before['records']['profile']),
                         {row[0] for row in MEMBERS})
        for key in before.keys() - {'records', 'revisions', 'coverage', 'academic_coverage'}:
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(list(get_user_model().objects.filter(pk__in=[row['id'] for row in users]).order_by('pk').values()), users)
        self.assertEqual(list(SessionContext.objects.order_by('pk').values()), sessions)
        self.assertEqual(list(NotificationRead.objects.order_by('pk').values()), reads)
        self.assertEqual(sha256(self.credentials_path.read_bytes()).hexdigest(), credentials)
        self.assertEqual(after['records']['profile']['SP-B']['group_id'], before['records']['profile']['SP-B']['group_id'])
        old_revision = before['revisions'][-1]['revision']
        for key in ('coverage', 'academic_coverage'):
            original = [{k: v for k, v in row.items() if k != 'revision__revision'}
                        for row in before[key] if row['revision__revision'] == old_revision]
            copied = [{k: v for k, v in row.items() if k != 'revision__revision'}
                      for row in after[key] if row['revision__revision'] == old_revision + 1]
            self.assertEqual(copied, original)
        self.assertEqual(GroupResource.objects.filter(demo_key__startswith='task11-directory-').count(), 2)

    def test_directory_omits_self_and_photos_require_current_permitted_audience(self):
        self.provision()
        self.login(self.b, 'student_b')
        directory = group_directory(current_actor(self.request()))
        self.assertEqual({row['name'] for row in directory}, {row[2] for row in MEMBERS})
        self.assertEqual(sum(bool(row['avatar_url']) for row in directory), 1)
        for row in directory:
            self.assertEqual(set(row), {'name', 'avatar_url'})
        permitted = Identity.objects.get(user__username='classmate_daria')
        restricted = Identity.objects.get(user__username='classmate_sofia')
        self.assertEqual(self.a.get(f'/people/{permitted.pk}/photo/').status_code, 200)
        self.assertEqual(self.a.get(f'/people/{restricted.pk}/photo/').status_code, 404)
        self.assertEqual(self.b.get(f'/people/{permitted.pk}/photo/').status_code, 404)
        self.assertTrue(Avatar.objects.filter(identity=restricted).exists())
        Preferences.objects.filter(user=permitted.user).update(photo_audience='private')
        self.assertEqual(self.a.get(f'/people/{permitted.pk}/photo/').status_code, 404)
        self.assertFalse(any(row['avatar_url'] for row in group_directory(current_actor(self.request()))))
        for _, username, _, _, _ in MEMBERS:
            self.assertFalse(get_user_model().objects.get(username=username).has_usable_password())

    def test_repeat_preserves_owner_changes_and_does_not_recreate_removed_avatar(self):
        self.provision()
        permitted = Identity.objects.get(user__username='classmate_daria')
        Preferences.objects.filter(user=permitted.user).update(photo_audience='private', contact_email='owner@example.invalid')
        Avatar.objects.filter(identity=permitted).delete()
        resource = GroupResource.objects.get(demo_key='task11-directory-materials')
        GroupResource.objects.filter(pk=resource.pk).update(title='Исправленная памятка сотрудника', version=2)
        before = canonical_state()
        self.provision(filename='unused-no-new-import.json')
        self.assertEqual(canonical_state(), before)
        self.assertFalse(Avatar.objects.filter(identity=permitted).exists())
        self.assertEqual(Preferences.objects.get(user=permitted.user).contact_email, 'owner@example.invalid')
        resource.refresh_from_db()
        self.assertEqual(resource.title, 'Исправленная памятка сотрудника')
        self.assertEqual(resource.version, 2)
        self.assertFalse((Path(self.temp.name) / 'unused-no-new-import.json').exists())

    def test_identity_collision_and_stale_revision_fail_before_import(self):
        before = canonical_state()
        get_user_model().objects.create_user(username='classmate_daria', password=None)
        with self.assertRaises(CommandError):
            self.provision()
        self.assertEqual(canonical_state(), before)
        with self.assertRaises(CommandError):
            call_command('provision_task11_directory', operator='isolated-task11', apply=True,
                         expected_revision=0, output=str(Path(self.temp.name) / 'stale.json'), stdout=StringIO())
        self.assertEqual(canonical_state(), before)

    def test_current_lesson_assessment_and_applicant_show_authorized_person_photo(self):
        teacher = Identity.objects.get(user__username='teacher_demo')
        Preferences.objects.create(user=teacher.user, photo_audience='context')
        Avatar.objects.create(identity=teacher, image=b'not-read-in-html-test', digest='a' * 64)
        lesson = Lesson.objects.filter(instructor_id=teacher.instructor_id, group__source_id='G-MB21').first()
        self.assertIsNotNone(lesson)
        self.assertContains(self.a.get(f'/lessons/{lesson.pk}/'), f'/people/{teacher.pk}/photo/')
        assessment = AssessmentEvent.objects.filter(instructor_id=teacher.instructor_id, group__source_id='G-MB21').first()
        self.assertIsNotNone(assessment)
        # Allowed teacher photo is based on a current assignment, independently of
        # immutable historical names that remain in the detail history.
        self.assertTrue(TeachingAssignment.objects.filter(teacher=teacher, group__source_id='G-MB21').exists())
        self.assertContains(self.a.get(f'/assessments/{assessment.pk}/'), f'/people/{teacher.pk}/photo/')
        student = current_actor(self.request()).identity
        Avatar.objects.create(identity=student, image=b'not-read-in-html-test', digest='b' * 64)
        self.assertContains(self.a.get('/requests/new/'), f'/people/{student.pk}/photo/')
