"""S5 HTTP recovery, authority and validation/render interleaving on test PG.

The validation-to-snapshot probe deliberately schedules another committed
decision between these two view operations; it is not a browser concurrency test.
"""
import re
from unittest.mock import patch

from django.db import DatabaseError
from django.test import Client, TransactionTestCase

from accounts.access import current_actor
from notifications.models import Notification, NotificationRead
from student_requests.models import Request, RequestEvent
from student_requests.services import transition_request
from student_requests import views as request_views
from tests import test_auth


class S5RequestViewTests(TransactionTestCase):
    login = test_auth.AuthTests.login
    request = test_auth.AuthTests.request
    page = test_auth.AuthTests.page
    marker = test_auth.AuthTests.marker

    def setUp(self):
        test_auth.AuthTests.setUp(self)
        self.login()
        self.login(self.b, 'student_b')
        self.staff = Client(enforce_csrf_checks=True)
        self.login(self.staff, 'staff_demo')

    def state(self):
        return (list(Request.objects.order_by('pk').values()),
                list(RequestEvent.objects.order_by('pk').values()),
                list(Notification.objects.order_by('pk').values()),
                list(NotificationRead.objects.order_by('pk').values()))

    def post(self, url, data, *, client=None, enhanced=False):
        client = client or self.a
        headers = {'HTTP_X_REQUEST_FORM': '1'} if enhanced else {}
        return client.post(url, {'csrfmiddlewaretoken': client.cookies['csrftoken'].value, **data}, **headers)

    def draft(self, client=None):
        page = (client or self.a).get('/requests/new/')
        self.assertEqual(page.status_code, 200)
        return {'context': page.context['marker'], 'submission_token': page.context['submission_token'],
                'request_type': 'study_certificate', 'comment': 'Для демонстрации'}

    def create(self, client=None, **changes):
        data = self.draft(client)
        data.update(changes)
        response = self.post('/requests/new/', data, client=client)
        self.assertEqual(response.status_code, 302)
        return Request.objects.get(pk=int(response.url.strip('/').split('/')[-1])), data

    def decision(self, row, action, explanation='', *, client=None):
        client = client or self.staff
        page = client.get(f'/staff/requests/{row.pk}/')
        self.assertEqual(page.status_code, 200)
        return {'context': page.context['marker'], 'expected_version': page.context['request_item']['version'],
                'action': action, 'explanation': explanation}

    def assert_error_links_exist(self, response):
        ids = set(re.findall(r'\bid="([^"]+)"', response.content.decode('utf-8')))
        self.assertTrue(response.context['error_summary'])
        for error in response.context['error_summary']:
            self.assertIn(error['target'], ids)

    def test_create_validation_correction_same_token_native_and_enhanced_saved_replay(self):
        before = self.state()
        draft = self.draft()
        self.assertEqual(self.state(), before)
        draft['comment'] = 'Я' * 501
        invalid = self.post('/requests/new/', draft)
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.context['submission_token'], draft['submission_token'])
        self.assertEqual(invalid.context['form']['comment'].value(), draft['comment'])
        self.assert_error_links_exist(invalid)
        self.assertEqual(self.state(), before)
        draft['comment'] = 'Исправленный комментарий'
        saved = self.post('/requests/new/', draft)
        self.assertEqual(saved.status_code, 302)
        self.assertRegex(saved.url, r'^/requests/[1-9][0-9]*/$')
        committed = self.state()
        replay = self.post('/requests/new/', draft, enhanced=True)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), {'state': 'saved', 'marker': draft['context'], 'redirect': saved.url})
        self.assertEqual(self.state(), committed)
        fresh = self.draft()
        fresh['comment'] = draft['comment']
        intentional = self.post('/requests/new/', fresh, enhanced=True)
        self.assertEqual(intentional.status_code, 200)
        self.assertNotEqual(intentional.json()['redirect'], saved.url)
        self.assertEqual(Request.objects.count(), 2)
        self.assertEqual(RequestEvent.objects.count(), 2)
        self.assertEqual(Notification.objects.filter(request_event__isnull=False).count(), 2)

    def test_unknown_type_and_accepted_changed_payload_never_change_first_request(self):
        draft = self.draft()
        invalid = self.post('/requests/new/', {**draft, 'request_type': 'real_certificate'})
        self.assertEqual(invalid.status_code, 400)
        self.assert_error_links_exist(invalid)
        self.assertEqual(Request.objects.count(), 0)
        saved = self.post('/requests/new/', draft)
        self.assertEqual(saved.status_code, 302)
        before = self.state()
        conflict = self.post('/requests/new/', {**draft, 'comment': 'Другой текст'})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.context['submission_token'], draft['submission_token'])
        self.assertContains(conflict, 'Проверить мои заявки', status_code=409)
        self.assertEqual(self.state(), before)

    def test_csrf_role_owner_faculty_status_never_grant_creation_authority(self):
        draft = self.draft()
        self.assertEqual(self.a.post('/requests/new/', draft).status_code, 403)
        forged = {**draft, 'owner': current_actor(self.request(self.b)).profile_id,
                  'role': 'staff', 'faculty': 'other', 'status': 'ready', 'version': 99}
        response = self.post('/requests/new/', forged)
        self.assertEqual(response.status_code, 302)
        row = Request.objects.get()
        actor = current_actor(self.request())
        self.assertEqual(row.owner_id, actor.profile_id)
        self.assertEqual(row.servicing_faculty_id, actor.identity.profile.group.programme.faculty_id)
        self.assertEqual((row.status, row.version), ('submitted', 1))
        before = self.state()
        staff_data = {**draft, 'context': current_actor(self.request(self.staff)).marker}
        self.assertEqual(self.post('/requests/new/', staff_data, client=self.staff).status_code, 403)
        anonymous = Client(enforce_csrf_checks=True)
        anonymous.get('/login/')
        denied = self.post('/requests/new/', draft, client=anonymous, enhanced=True)
        self.assertEqual(denied.status_code, 401)
        self.assertNotIn('redirect', denied.json())
        self.assertEqual(self.state(), before)

    def test_stale_context_wins_over_invalid_fields_and_accepted_replay_after_account_change(self):
        row, accepted = self.create()
        before = self.state()
        self.assertEqual(self.post('/logout/', {'context': accepted['context']}).status_code, 302)
        self.login(self.a, 'student_b')
        # Current CSRF deliberately accompanies stale A context and accepted key.
        for comment in (accepted['comment'], 'Я' * 501):
            response = self.post('/requests/new/', {**accepted, 'comment': comment}, enhanced=True)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json(), {'state': 'stale_context'})
        self.assertEqual(self.state(), before)
        self.assertEqual(self.a.get(f'/requests/{row.pk}/').status_code, 404)
        b_marker = current_actor(self.request()).marker
        self.assertEqual(self.post('/logout/', {'context': b_marker}).status_code, 302)
        self.login()
        response = self.post('/requests/new/', accepted, enhanced=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['state'], 'stale_context')
        self.assertEqual(self.state(), before)

    def test_staff_validation_retains_only_chosen_form_then_commits_corrected_decision(self):
        row, _ = self.create()
        take = self.decision(row, 'take')
        self.assertEqual(self.post(f'/staff/requests/{row.pk}/', take, client=self.staff).status_code, 302)
        row.refresh_from_db()
        invalid_text = 'Длинное пояснение: ' + 'Я' * 501
        data = self.decision(row, 'complete', invalid_text)
        before = self.state()
        invalid = self.post(f'/staff/requests/{row.pk}/', data, client=self.staff)
        self.assertEqual(invalid.status_code, 400)
        self.assert_error_links_exist(invalid)
        forms = {action['action']: action for action in invalid.context['actions']}
        self.assertEqual(forms['complete']['form']['explanation'].value(), invalid_text)
        self.assertTrue(forms['complete']['expanded'])
        self.assertFalse(forms['reject']['form'].is_bound)
        self.assertEqual(self.state(), before)
        valid = self.post(f'/staff/requests/{row.pk}/', {**data, 'explanation': 'Демо: обработка завершена.'},
                          client=self.staff, enhanced=True)
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.json()['state'], 'saved')
        row.refresh_from_db()
        self.assertEqual((row.status, row.version), ('ready', 3))
        self.assertEqual(RequestEvent.objects.filter(request=row).count(), 3)
        self.assertEqual(Notification.objects.filter(request_event__request=row).count(), 3)
        terminal = self.staff.get(f'/staff/requests/{row.pk}/')
        self.assertEqual(terminal.context['actions'], [])

    def test_other_commit_between_validation_and_snapshot_requires_fresh_choice(self):
        row, _ = self.create()
        data = self.decision(row, 'reject', 'Старое пояснение ' + 'Я' * 501)
        original_snapshot = request_views.snapshot_for_form
        competitor_request = self.request(self.staff)
        competitor_marker = current_actor(competitor_request).marker

        def interleave(request, section, **kwargs):
            transition_request(competitor_request, row.pk, competitor_marker, 'take', 1)
            return original_snapshot(request, section, **kwargs)

        with patch('student_requests.views.snapshot_for_form', side_effect=interleave):
            response = self.post(f'/staff/requests/{row.pk}/', data, client=self.staff)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.context['actions'], [])
        self.assertEqual(response.context['request_item']['version'], 2)
        self.assertNotContains(response, 'Старое пояснение', status_code=409)
        self.assertContains(response, 'Проверить актуальную заявку', status_code=409)
        self.assertEqual(RequestEvent.objects.filter(request=row).count(), 2)
        self.assertEqual(Notification.objects.filter(request_event__request=row).count(), 2)

    def test_malformed_version_and_irrelevant_explanation_link_to_visible_nonfield_target(self):
        row, _ = self.create()
        data = self.decision(row, 'take')
        before = self.state()
        for changes in ({'expected_version': 'bad'}, {'explanation': 'У этого действия нет поля объяснения'}):
            response = self.post(f'/staff/requests/{row.pk}/', {**data, **changes}, client=self.staff)
            self.assertEqual(response.status_code, 400)
            self.assert_error_links_exist(response)
            self.assertEqual(self.state(), before)

    def test_staff_old_context_with_invalid_decision_never_returns_input_or_applicant(self):
        row, _ = self.create()
        data = self.decision(row, 'reject', 'Приватное старое пояснение ' + 'Я' * 501)
        before = self.state()
        self.assertEqual(self.post('/logout/', {'context': data['context']}, client=self.staff).status_code, 302)
        self.login(self.staff, 'staff_demo')
        response = self.post(f'/staff/requests/{row.pk}/', data, client=self.staff, enhanced=True)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'state': 'stale_context'})
        self.assertNotIn('Приватное', response.content.decode('utf-8'))
        self.assertEqual(self.state(), before)

    def test_detail_list_history_and_notifications_get_are_read_only_and_scoped(self):
        row, _ = self.create()
        event = Notification.objects.get(request_event__request=row)
        before = self.state()
        for _ in range(2):
            for path in ('/requests/', f'/requests/{row.pk}/', f'/requests/{row.pk}/#request-history',
                         '/notifications/?kind=requests', f'/notifications/{event.pk}/'):
                self.assertEqual(self.a.get(path).status_code, 200)
        self.assertEqual(self.b.get(f'/requests/{row.pk}/').status_code, 404)
        self.assertEqual(self.b.get(f'/notifications/{event.pk}/').status_code, 404)
        self.assertEqual(self.staff.get(f'/requests/{row.pk}/').status_code, 403)
        self.assertEqual(self.staff.get('/study/').status_code, 403)
        self.assertEqual(self.a.get('/staff/').status_code, 403)
        self.assertEqual(self.a.get(f'/staff/requests/{row.pk}/').status_code, 403)
        self.assertEqual(self.staff.get('/staff/requests/999999999/').status_code, 404)
        foreign_detail = self.b.get('/requests/')
        self.assertEqual(foreign_detail.context['request_count'], 0)
        queue = self.staff.get('/staff/?status=submitted')
        self.assertEqual((queue.context['request_count'], queue.context['filtered_count']), (1, 1))
        self.assertEqual(self.staff.get('/staff/?status=unknown').status_code, 400)
        self.assertEqual(self.state(), before)

    def test_unconfirmed_save_response_has_no_false_success_or_automatic_new_key(self):
        draft = self.draft()
        before = self.state()
        with patch('student_requests.views.submit_request', side_effect=DatabaseError('isolated failure')):
            enhanced = self.post('/requests/new/', draft, enhanced=True)
            ordinary = self.post('/requests/new/', draft)
        self.assertEqual(enhanced.status_code, 503)
        self.assertEqual(enhanced.json(), {'state': 'save_unconfirmed'})
        self.assertContains(ordinary, 'Не удалось подтвердить сохранение', status_code=503)
        self.assertNotContains(ordinary, 'isolated failure', status_code=503)
        self.assertEqual(self.state(), before)
