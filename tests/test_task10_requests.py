"""TASK10: типизированные заявки, старые ключи и scoped SQL pagination."""
from copy import deepcopy
import uuid
from unittest.mock import patch

from django.core import signing
from django.db import connection, IntegrityError, transaction
from django.test import Client, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from accounts.access import AccessDenied, current_actor, logout_session
from data_import.services import apply
from data_import.state import canonical_state
from notifications.models import Notification
from student_requests.models import Request, RequestEvent
from student_requests.selectors import request_page
from student_requests.services import (RequestError, fingerprint, issue_submission,
                                       submit_request, transition_request)
from tests import test_auth


class TypedRequestTests(TransactionTestCase):
    setUp = test_auth.AuthTests.setUp
    login = test_auth.AuthTests.login
    request = test_auth.AuthTests.request
    page = test_auth.AuthTests.page
    post = test_auth.AuthTests.post
    marker = test_auth.AuthTests.marker
    scope_change = test_auth.AuthTests.scope_change

    def actors(self):
        self.login()
        self.staff = Client(enforce_csrf_checks=True)
        self.login(self.staff, 'staff_demo')
        return current_actor(self.request())

    def fields(self, kind='study_certificate'):
        if kind == 'study_certificate':
            return {'purpose': 'employment', 'recipient': 'Учебная организация (демо)', 'copies': '1'}
        return {'correction_area': 'result', 'record_label': 'Иностранный язык',
                'requested_change': 'Прошу проверить указанный результат. Синтетический пример.'}

    def create(self, *, kind='study_certificate', fields=None, comment='', token=None, client=None):
        req = self.request(client)
        actor = current_actor(req)
        return submit_request(req, actor.marker, token or issue_submission(actor, payload_schema=2), kind,
                              comment, fields=self.fields(kind) if fields is None else fields)

    def change(self, row, action, explanation=''):
        req = self.request(self.staff)
        actor = current_actor(req)
        result = transition_request(req, row.pk, actor.marker, action, row.version, explanation)
        row.refresh_from_db()
        return result

    def test_two_typed_payloads_preserve_source_and_server_ownership(self):
        actor = self.actors()
        source = canonical_state()
        for kind in ('study_certificate', 'academic_correction'):
            result = self.create(kind=kind)
            row = Request.objects.get(pk=result['id'])
            self.assertEqual(row.payload_schema, 2)
            self.assertEqual(row.owner_id, actor.profile_id)
            self.assertEqual(row.servicing_faculty_id, actor.identity.profile.group.programme.faculty_id)
            self.assertEqual(row.applicant_snapshot['group'], actor.identity.profile.group.name)
            self.change(row, 'take')
            self.change(row, 'complete', 'Проверено в демонстрации; учебный импорт не изменён.')
            self.assertEqual(row.events.count(), 3)
            self.assertEqual(Notification.objects.filter(request_event__request=row).count(), 3)
            expected_label = ('Справка об обучении' if kind == 'study_certificate' else 'Исправление учебных данных')
            for path in ('/notifications/?kind=requests', '/notifications/?kind=requests&archive=1'):
                response = self.page(path=path)
                item = next(item for item in response.context['notifications'] if item['current']['id'] == row.pk)
                self.assertEqual(item['type_label'], expected_label)
                self.assertEqual(item['current']['type_label'], expected_label)
                if kind == 'academic_correction':
                    self.assertIn('только проверенным импортом', item['ready_notice'])
                    self.assertNotIn('справка', item['ready_notice'])
                self.assertContains(response, expected_label)
        self.assertEqual(canonical_state(), source)

    def test_validation_rejects_missing_unrelated_and_invalid_values(self):
        actor = self.actors()
        token = issue_submission(actor, payload_schema=2)
        invalid = [({}, 'study_certificate'),
                   ({**self.fields(), 'copies': '0'}, 'study_certificate'),
                   ({**self.fields(), 'copies': '4'}, 'study_certificate'),
                   ({**self.fields(), 'purpose': 'invented'}, 'study_certificate'),
                   ({**self.fields(), 'recipient': 'x' * 161}, 'study_certificate'),
                   ({**self.fields(), 'owner': str(actor.profile_id)}, 'study_certificate'),
                   ({**self.fields('academic_correction'), 'requested_change': ' \n\t'}, 'academic_correction'),
                   ({**self.fields('academic_correction'), 'record_label': 'x\x00y'}, 'academic_correction')]
        for fields, kind in invalid:
            with self.subTest(kind=kind, fields=fields), self.assertRaises(RequestError):
                self.create(kind=kind, fields=fields, token=token)
            self.assertEqual(Request.objects.count(), 0)
        self.create(token=token)
        self.assertEqual(Request.objects.count(), 1)

    def test_v2_replay_normalizes_all_fields_and_detects_each_conflict(self):
        actor = self.actors()
        token = issue_submission(actor, payload_schema=2)
        first = self.create(token=token, comment='  Комментарий\r\n ',
                            fields={**self.fields(), 'copies': '01', 'recipient': '  Получатель\r\nдемо  '})
        fields = {**self.fields(), 'copies': '1', 'recipient': 'Получатель\nдемо'}
        replay = self.create(token=token, comment='Комментарий', fields=fields)
        self.assertEqual(first['id'], replay['id'])
        self.assertFalse(replay['created'])
        for changed in ({**fields, 'copies': '2'}, {**fields, 'recipient': 'Другой'},
                        {**fields, 'purpose': 'other'}):
            with self.assertRaises(RequestError) as caught:
                self.create(token=token, comment='Комментарий', fields=changed)
            self.assertEqual(caught.exception.code, 'payload_conflict')
        with self.assertRaises(RequestError):
            self.create(token=token, comment='Изменён', fields=fields)
        with self.assertRaises(RequestError):
            self.create(token=token, kind='academic_correction')
        self.assertEqual(Request.objects.count(), 1)
        self.assertEqual(RequestEvent.objects.count(), 1)

    def test_legacy_signed_token_and_fingerprint_replay_keep_original_interpretation(self):
        actor = self.actors()
        key = uuid.uuid4()
        token = signing.dumps({'key': str(key), 'user': actor.user_id, 'owner': actor.profile_id,
                               'context': str(actor.context_id), 'marker': actor.marker},
                              salt='student-request-submission-v1')
        args = (self.request(), actor.marker, token, 'study_certificate', 'Старый комментарий')
        created = submit_request(*args)
        row = Request.objects.get(pk=created['id'])
        original = deepcopy(row.__dict__)
        self.assertEqual(row.payload_schema, 1)
        self.assertEqual(row.payload_data, {})
        self.assertEqual(row.applicant_snapshot, {})
        self.assertEqual(row.payload_fingerprint, fingerprint('study_certificate', 'Старый комментарий'))
        self.assertFalse(submit_request(*args)['created'])
        row.refresh_from_db()
        for name in ('payload_schema', 'payload_data', 'applicant_snapshot', 'payload_fingerprint', 'submission_key'):
            self.assertEqual(getattr(row, name), original[name])
        with self.assertRaises(RequestError):
            submit_request(*args, fields=self.fields())

    def test_new_payload_and_profile_snapshot_are_database_immutable(self):
        self.actors()
        row = Request.objects.get(pk=self.create()['id'])
        before = deepcopy(row.applicant_snapshot)
        for change in ({'payload_schema': 1}, {'payload_data': {}}, {'applicant_snapshot': {}},
                       {'payload_data': {**row.payload_data, 'copies': 3}}):
            with self.subTest(change=change), self.assertRaises(IntegrityError), transaction.atomic():
                Request.objects.filter(pk=row.pk).update(**change)
        apply(self.scope_change(), expected_revision=4, operator='task10-request-snapshot-test')
        row.refresh_from_db()
        self.assertEqual(row.applicant_snapshot, before)
        self.assertNotEqual(row.owner.group.name, row.applicant_snapshot['group'])

    def test_sql_pagination_scopes_filters_ties_and_out_of_range(self):
        actor = self.actors()
        now = timezone.now()
        with patch('student_requests.services.timezone.now', return_value=now):
            ids = [self.create()['id'] for _ in range(21)]
        self.login(self.b, 'student_b')
        foreign = self.create(client=self.b)['id']
        with CaptureQueriesContext(connection) as captured:
            second = request_page(actor, page_number=2)
        self.assertEqual(second['request_count'], 21)
        self.assertEqual([row['id'] for row in second['requests']], list(reversed(ids))[10:20])
        self.assertTrue(any('LIMIT 10 OFFSET 10' in query['sql'] for query in captured.captured_queries))
        last = request_page(actor, page_number=99)
        self.assertEqual(last['page_obj'].number, 3)
        self.assertEqual(len(last['requests']), 1)
        self.assertTrue(last['page_notice'])
        self.assertNotIn(foreign, [row['id'] for row in second['requests']])
        row = Request.objects.get(pk=ids[0])
        self.change(row, 'reject', 'Синтетическая причина')
        filtered = request_page(current_actor(self.request(self.staff)), status_filter='rejected')
        self.assertEqual(filtered['request_count'], 1)
        self.assertEqual([item['id'] for item in filtered['requests']], [row.pk])
        self.assertEqual(request_page(actor, status_filter='ready')['requests'], [])
        staff_page = request_page(current_actor(self.request(self.staff)), page_number=2)
        self.assertEqual(staff_page['request_count'], 22)
        self.assertEqual(len(staff_page['requests']), 10)

    def test_http_type_forms_validation_values_and_filter_links(self):
        self.actors()
        response = self.page(path='/requests/new/?type=academic_correction')
        self.assertContains(response, 'name="requested_change"')
        self.assertNotContains(response, 'name="recipient"')
        token, marker = response.context['submission_token'], response.context['marker']
        invalid = self.post('/requests/new/', context=marker, submission_token=token,
                            request_type='academic_correction', correction_area='result',
                            record_label='Дисциплина сохранена', requested_change='', comment='Мой комментарий')
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, 'Дисциплина сохранена', status_code=400)
        self.assertContains(invalid, 'Мой комментарий', status_code=400)
        self.assertEqual(Request.objects.count(), 0)
        valid = self.post('/requests/new/', context=marker, submission_token=token,
                          request_type='academic_correction', **self.fields('academic_correction'))
        self.assertEqual(valid.status_code, 302)
        self.assertContains(self.a.get(valid.url), 'Что нужно проверить и исправить')
        for path in ('/requests/?page=0', '/requests/?page=x', '/requests/?status=unknown', '/staff/?page=-1'):
            client = self.staff if path.startswith('/staff') else self.a
            self.assertEqual(client.get(path).status_code, 400)
        self.assertEqual(self.a.get('/requests/?page=999').status_code, 200)

    def test_typed_payload_cross_account_and_stale_form_are_denied(self):
        actor = self.actors()
        token = issue_submission(actor, payload_schema=2)
        first = self.create(token=token)
        self.login(self.b, 'student_b')
        with self.assertRaises(AccessDenied):
            self.create(token=token, client=self.b)
        self.assertEqual(self.b.get(f"/requests/{first['id']}/").status_code, 404)
        logout_session(self.request(), actor.marker)
        self.login()
        with self.assertRaises(AccessDenied):
            self.create(token=token)
        self.assertEqual(Request.objects.count(), 1)
