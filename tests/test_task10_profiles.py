"""TASK10: личные настройки, безопасное фото, минимальная группа и публикации."""
from copy import deepcopy
from datetime import timedelta
from io import BytesIO

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TransactionTestCase
from django.utils import timezone
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from accounts.access import AccessDenied, current_actor, logout_session
from accounts.models import Identity
from data_import.contracts import validate
from data_import.services import apply
from data_import.state import canonical_state
from notifications.delivery import delivery_actor
from profiles.forms import ProfileForm, ResourceForm, safe_image
from profiles.models import Avatar, GroupResource, GroupResourceRevision, Preferences
from profiles.selectors import can_see_person, group_directory, group_resources, publisher_groups
from profiles.services import save_resource, update_profile
from studies.models import Group, StudentProfile, Enrollment, Instructor
from tests import test_auth
from tests.test_import import signed
from tests import test_s4_reads


def png_upload(size=(64, 64), *, trailing=False):
    output = BytesIO()
    metadata = PngInfo()
    metadata.add_text('private', 'Secret metadata sentinel')
    Image.new('RGBA', size, (34, 62, 91, 128)).save(output, format='PNG', pnginfo=metadata)
    raw = output.getvalue() + (b'<script>payload-sentinel</script>' if trailing else b'')
    return SimpleUploadedFile('synthetic.png', raw, content_type='image/png')


class AvatarValidationTests(SimpleTestCase):
    def test_reencoding_bounds_strips_metadata_and_trailing_content(self):
        sanitized = safe_image(png_upload((1000, 700), trailing=True))
        self.assertNotIn(b'Secret metadata sentinel', sanitized)
        self.assertNotIn(b'payload-sentinel', sanitized)
        with Image.open(BytesIO(sanitized)) as image:
            self.assertEqual(image.format, 'JPEG')
            self.assertEqual(image.mode, 'RGB')
            self.assertEqual(image.size, (512, 358))
            self.assertFalse(image.getexif())

    def test_svg_animation_oversize_dimensions_and_corruption_are_rejected(self):
        gif = BytesIO()
        Image.new('RGB', (64, 64), 'red').save(gif, format='GIF', save_all=True,
            append_images=[Image.new('RGB', (64, 64), 'blue')], duration=100, loop=0)
        uploads = [SimpleUploadedFile('fake.png', b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', content_type='image/png'),
                   SimpleUploadedFile('large.jpg', b'0' * (2*1024*1024+1), content_type='image/jpeg'),
                   SimpleUploadedFile('motion.gif', gif.getvalue(), content_type='image/gif'),
                   png_upload((31, 64)), png_upload((4097, 32)), png_upload((4096, 4096)),
                   SimpleUploadedFile('broken.png', png_upload().read()[:30], content_type='image/png')]
        for upload in uploads:
            with self.subTest(name=upload.name, length=upload.size), self.assertRaises(ValidationError):
                safe_image(upload)


class ProfileAndGroupTests(TransactionTestCase):
    setUp = test_auth.AuthTests.setUp
    login = test_auth.AuthTests.login
    request = test_auth.AuthTests.request
    page = test_auth.AuthTests.page
    post = test_auth.AuthTests.post
    marker = test_auth.AuthTests.marker
    scope_change = test_auth.AuthTests.scope_change

    def actor(self, client=None):
        return current_actor(self.request(client))

    def start(self):
        self.login()
        self.login(self.b, 'student_b')
        self.staff = Client(enforce_csrf_checks=True)
        self.login(self.staff, 'staff_demo')
        return self.actor()

    def profile_data(self, **changes):
        pref = Preferences.objects.filter(user_id=self.actor().user_id).first()
        return {'version': pref.version if pref else 0, 'contact_email': '', 'email_enabled': False,
                'email_requests': True, 'email_academic': True, 'email_announcements': True,
                'photo_audience': 'private', **changes}

    def synthetic_directory(self):
        """Новые тестовые source records принимаются обычным версионным импортом."""
        payload = deepcopy(self.scope_change().payload)
        source = payload['source_as_of']
        records = [
            ('faculty', {'source_id': 'F-T10-OTHER', 'name': 'Другой факультет (демо)'}),
            ('programme', {'source_id': 'PR-T10-OTHER', 'name': 'Другая программа (демо)', 'faculty_id': 'F-T10-OTHER'}),
            ('group', {'source_id': 'G-T10-OTHER', 'name': 'ДР-11 (демо)', 'programme_id': 'PR-T10-OTHER'}),
            ('profile', {'source_id': 'SP-T10-PEER', 'name': 'Синтетический одногруппник', 'year': 2, 'group_id': 'G-MB21'}),
        ]
        payload['operations'] = [{'operation': 'create', 'entity_type': kind,
            'record': {'record_revision': 1, 'source_updated_at': source, **record}} for kind, record in records]
        apply(validate(signed(payload)), expected_revision=4, operator='task10-directory-fixture')
        peer = get_user_model().objects.create_user('peer_demo', password='Only-isolated-test-password')
        identity = Identity.objects.create(user=peer, role='student', profile=StudentProfile.objects.get(source_id='SP-T10-PEER'))
        Preferences.objects.create(user=peer, photo_audience='context', contact_email='private-peer@example.invalid')
        return identity

    def test_owner_profile_upload_replace_remove_preserve_institutional_state(self):
        actor = self.start()
        original = canonical_state()
        response = self.post('/profile/edit/', context=actor.marker, **self.profile_data(),
            photo=png_upload(), role='staff', group=self.actor(self.b).group_id, name='Подмена')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(canonical_state(), original)
        actor = self.actor()
        self.assertEqual(actor.role, 'student')
        image = Avatar.objects.get(identity=actor.identity)
        first_digest = image.digest
        url = f'/people/{actor.identity.pk}/photo/'
        response = self.a.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'image/jpeg')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.b.get(url).status_code, 404)
        self.assertEqual(self.staff.get(url).status_code, 404)
        self.assertEqual(self.post('/profile/edit/', context=actor.marker,
            **self.profile_data(), photo=png_upload((96, 64))).status_code, 302)
        image.refresh_from_db()
        self.assertNotEqual(image.digest, first_digest)
        self.assertEqual(self.post('/profile/edit/', context=actor.marker,
            **self.profile_data(remove_photo=True)).status_code, 302)
        self.assertEqual(self.a.get(url).status_code, 404)
        self.assertFalse(Avatar.objects.exists())

    def test_group_directory_has_minimal_fields_and_photo_audience_is_enforced(self):
        self.start()
        peer = self.synthetic_directory()
        Avatar.objects.create(identity=peer, image=safe_image(png_upload()), digest='a'*64)
        actor = self.actor()
        directory = group_directory(actor)
        # UI review: the owner belongs in Profile, not in their own classmate list.
        self.assertEqual({item['name'] for item in directory}, {'Синтетический одногруппник'})
        self.assertNotIn(actor.identity.profile.name, {item['name'] for item in directory})
        for item in directory:
            self.assertEqual(set(item), {'name', 'avatar_url'})
        self.assertTrue(next(item for item in directory if item['name'] == peer.profile.name)['avatar_url'])
        url = f'/people/{peer.pk}/photo/'
        self.assertEqual(self.a.get(url).status_code, 200)
        self.assertEqual(self.b.get(url).status_code, 404)
        self.assertEqual(self.staff.get(url).status_code, 404)
        Preferences.objects.filter(user=peer.user).update(photo_audience='private')
        self.assertEqual(self.a.get(url).status_code, 404)
        page = self.a.get('/profile/group/')
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'private-peer@example.invalid')
        self.assertNotContains(page, 'Тимур Светлов')

    def resource_data(self, group, **changes):
        now = timezone.now()
        return {'group': group.pk, 'category': 'course', 'title': 'Синтетический курс',
                'description': 'Иллюстративный ресурс группы, не действующее предложение.',
                'url': 'https://example.invalid/course', 'available_from': now-timedelta(minutes=1),
                'available_until': now+timedelta(days=1), 'version': 0, **changes}

    def test_staff_resource_scope_versions_history_and_availability(self):
        self.start()
        self.synthetic_directory()
        staff_actor = self.actor(self.staff)
        student_actor = self.actor()
        group = student_actor.identity.profile.group
        groups = publisher_groups(staff_actor)
        form = ResourceForm(self.resource_data(group), groups=groups)
        self.assertTrue(form.is_valid(), form.errors)
        pk = save_resource(self.request(self.staff), staff_actor.marker, form.cleaned_data)
        row = GroupResource.objects.get(pk=pk)
        now = timezone.now()
        self.assertEqual(list(group_resources(student_actor, now).values_list('pk', flat=True)), [pk])
        self.assertFalse(group_resources(self.actor(self.b), now).filter(pk=pk).exists())
        self.assertFalse(group_resources(student_actor, row.available_until).filter(pk=pk).exists())
        updated = ResourceForm(self.resource_data(group, version=1, title='Обновлённый синтетический курс'),
                               groups=groups, instance=row)
        self.assertTrue(updated.is_valid(), updated.errors)
        save_resource(self.request(self.staff), staff_actor.marker, updated.cleaned_data, pk)
        self.assertEqual(GroupResourceRevision.objects.filter(resource_id=pk).count(), 2)
        self.assertEqual(GroupResourceRevision.objects.get(resource_id=pk, version=1).payload['title'], 'Синтетический курс')
        with self.assertRaises(AccessDenied):
            save_resource(self.request(self.staff), staff_actor.marker, updated.cleaned_data, pk)
        with self.assertRaises(AccessDenied):
            save_resource(self.request(), student_actor.marker, form.cleaned_data)
        foreign = Group.objects.get(source_id='G-T10-OTHER')
        denied = ResourceForm(self.resource_data(foreign), groups=groups)
        self.assertFalse(denied.is_valid())
        with self.assertRaises(AccessDenied):
            save_resource(self.request(self.staff), staff_actor.marker, {**form.cleaned_data, 'group': foreign})
        wrong_url = ResourceForm(self.resource_data(group, url='http://example.invalid'), groups=groups)
        self.assertFalse(wrong_url.is_valid())

    def test_profile_invalid_values_and_stale_version_or_session_keep_saved_state(self):
        actor = self.start()
        initial = self.profile_data(contact_email='profile@example.invalid', email_enabled=True)
        form = ProfileForm(initial)
        self.assertTrue(form.is_valid(), form.errors)
        saved = update_profile(self.request(), actor.marker, form.cleaned_data)
        with self.assertRaises(AccessDenied):
            update_profile(self.request(), actor.marker, form.cleaned_data)
        invalid = self.post('/profile/edit/', context=actor.marker,
                            **self.profile_data(contact_email='not-an-email', email_enabled=True))
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, 'not-an-email', status_code=400)
        saved.refresh_from_db()
        self.assertEqual(saved.contact_email, 'profile@example.invalid')
        stale = self.profile_data(contact_email='stale@example.invalid')
        logout_session(self.request(), actor.marker)
        self.login()
        response = self.post('/profile/edit/', context=actor.marker, **stale)
        self.assertEqual(response.status_code, 409)
        self.assertNotContains(response, 'stale@example.invalid', status_code=409)
        saved.refresh_from_db()
        self.assertEqual(saved.contact_email, 'profile@example.invalid')


class TeacherPhotoAudienceTests(TransactionTestCase):
    setUp = test_s4_reads.S4ReadTests.setUp
    accept = test_s4_reads.S4ReadTests.accept
    candidate = test_s4_reads.S4ReadTests.candidate
    source = test_s4_reads.S4ReadTests.source
    withdraw = test_s4_reads.S4ReadTests.withdraw
    request = test_s4_reads.S4ReadTests.request

    def test_teacher_photo_audience_ends_when_enrollment_withdrawn(self):
        from materials.models import TeachingAssignment
        user = get_user_model().objects.create_user('photo_teacher', password='Only-isolated-teacher-password')
        identity = Identity.objects.create(user=user, role='teacher', instructor=Instructor.objects.first())
        enrollment = Enrollment.objects.get(source_id='E-A-1')
        now = timezone.now()
        TeachingAssignment.objects.create(teacher=identity, course=enrollment.course,
            period=enrollment.period, group=enrollment.student.group,
            available_from=now-timedelta(hours=1), available_until=now+timedelta(days=1))
        student = Identity.objects.get(profile=enrollment.student)
        Preferences.objects.create(user=student.user, photo_audience='context')
        self.assertTrue(can_see_person(delivery_actor(user.pk, now), student))
        self.accept(self.withdraw())
        self.assertFalse(can_see_person(delivery_actor(user.pk, timezone.now()), student))
