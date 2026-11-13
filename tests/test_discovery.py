"""Поиск по PostgreSQL: граница доступа раньше совпадений, read-only ответы."""
import json
from datetime import timedelta
from unittest.mock import patch
from django.test import Client, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone
from accounts.access import current_actor
from accounts.models import SessionContext
from data_import.state import canonical_state
from data_import.models import ImportRevision
from discovery.catalog import actions, entry, normalize, parse_query, ranked, reference_id
from notifications.models import NotificationRead, NotificationReceipt
from student_requests.models import Request
from student_requests.services import issue_submission, submit_request
from tests import test_task10_materials as legacy
from tests import test_task16 as workspaces_legacy


class MatchingTests(SimpleTestCase):
    def test_verified_action_precedes_repeated_service_records(self):
        query, tokens, _ = parse_query('справка')
        rows = actions('student') + [entry(f'request:{number}',
            f'З-{number:06d} · Справка об обучении', 'Заявка', 'Готова', f'/requests/{number}/')
            for number in range(1, 31)]
        results = ranked(rows, query, tokens)
        self.assertEqual(results[0]['key'], 'action:certificate')
        self.assertEqual(len(results), 31)
        self.assertEqual([row['key'] for row in results], [row['key'] for row in ranked(list(reversed(rows)), query, tokens)])

    def test_russian_case_word_matching_synonyms_and_stable_order(self):
        self.assertEqual(normalize('  УЧЁБА  '), 'учеба')
        for query, key in (('СПРАВКУ', 'certificate'), ('Learning Agreement', 'exchange'),
                           ('приём', 'consultation'), ('оценки', 'grades')):
            normalized, tokens, _ = parse_query(query)
            self.assertIn('action:' + key, [item['key'] for item in ranked(actions('student'), normalized, tokens)])
        rows = [entry('b', 'Экономика', 'Дисциплина', 'Семестр', '/b/'),
                entry('a', 'Экономика', 'Дисциплина', 'Семестр', '/a/')]
        query, tokens, _ = parse_query('эконом')
        self.assertEqual([item['key'] for item in ranked(rows, query, tokens)], ['a', 'b'])
        self.assertEqual(reference_id('з-000042'), 42)
        self.assertIsNone(reference_id('справка 42'))

    def test_query_and_page_have_explicit_bounds(self):
        for value, page in [('x' * 101, '1'), ('x', '0'), ('x', '101'), ('x', '1.5'),
                            ('один два три четыре пять шесть семь восемь девять', '1')]:
            with self.assertRaises(ValueError):
                parse_query(value, page)


@override_settings(SUPPORT_MODEL='', SUPPORT_LOCAL_ONLY_CONFIRMED=False)
class SearchAccessTests(TransactionTestCase):
    setUp = legacy.MaterialTests.setUp
    login = legacy.MaterialTests.login
    request = legacy.MaterialTests.request
    page = legacy.MaterialTests.page
    post = legacy.MaterialTests.post
    marker = legacy.MaterialTests.marker
    publish = workspaces_legacy.WorkspaceTests.publish

    def search(self, query, client=None, **extra):
        client = client or self.a
        actor = current_actor(self.request(client))
        response = client.post('/search/results/', {'q': query, 'page': '1', 'context': actor.marker,
            'csrfmiddlewaretoken': client.cookies['csrftoken'].value, **extra},
            HTTP_X_COMPONENT='1', HTTP_X_PAGE_CONTEXT=actor.marker)
        self.assertEqual(response.status_code, 200, response.content[:400])
        self.assertIn('no-store', response['Cache-Control'])
        return response.json()

    def create_request(self, client=None, comment='Личное сообщение не входит в поиск'):
        request = self.request(client)
        actor = current_actor(request)
        result = submit_request(request, actor.marker, issue_submission(actor), 'study_certificate', comment)
        return Request.objects.get(pk=result['id'])

    def test_actions_courses_and_exact_reference_with_ai_absent_read_no_state(self):
        row = self.create_request()
        before = canonical_state()
        reads = NotificationRead.objects.count()
        receipts = list(NotificationReceipt.objects.order_by('pk').values())
        for query, url in [('справка', '/requests/new/?type=study_certificate'),
                           ('обмен', '/journeys/?template=exchange'),
                           ('консультация', '/consultations/')]:
            self.assertIn(url, [item['url'] for item in self.search(query)['items']])
        data = self.search(row.reference)
        self.assertEqual(data['count'], 1)
        self.assertEqual(data['items'][0]['url'], f'/requests/{row.pk}/')
        self.assertEqual(self.search('Личное сообщение')['count'], 0)
        name = self.assignment.course.name
        self.assertTrue(any(item['category'] == 'Дисциплина' for item in self.search(name)['items']))
        self.assertEqual(canonical_state(), before)
        self.assertEqual(NotificationRead.objects.count(), reads)
        self.assertEqual(list(NotificationReceipt.objects.order_by('pk').values()), receipts)

    def test_foreign_reference_and_teacher_administrative_cases_are_indistinguishable(self):
        row = self.create_request()
        self.login(self.b, 'student_b')
        foreign = self.search(row.reference, self.b)
        absent = self.search('З-999999999999', self.b)
        self.assertEqual(foreign, absent)
        teacher = self.search(row.reference, self.teacher)
        self.assertEqual(teacher['count'], 0)
        self.assertNotIn('requests', [source['key'] for source in teacher['sources']])
        staff = Client(enforce_csrf_checks=True)
        self.login(staff, 'staff_demo')
        data = self.search(self.assignment.course.name, staff)
        self.assertFalse(any(item['category'] == 'Дисциплина' for item in data['items']))
        self.assertNotIn('academic', [source['key'] for source in data['sources']])

    def test_published_current_titles_only_scope_before_count(self):
        row, _ = self.publish(title='Публикация для поиска', body='Приватное тело не индексируется')
        self.assertEqual(self.search('Публикация для поиска')['count'], 1)
        self.assertEqual(self.search('Приватное тело')['count'], 0)
        self.login(self.b, 'student_b')
        self.assertEqual(self.search('Публикация для поиска', self.b)['count'], 0)
        row, _ = self.publish(row=row, action='save', title='Только будущий черновик')
        self.assertEqual(self.search('Только будущий')['count'], 0)
        self.assertEqual(self.search('Только будущий', self.teacher)['count'], 0)
        self.assertEqual(self.search('Публикация для поиска')['count'], 1)
        row, _ = self.publish(row=row, action='withdraw')
        self.assertEqual(self.search('Публикация для поиска')['count'], 0)

    def test_unavailable_academic_source_is_not_reported_as_empty_success(self):
        after_validity = ImportRevision.objects.order_by('-valid_until').first().valid_until + timedelta(seconds=1)
        # Only the fact-reading clock is advanced; source validity is the real SQL predicate.
        with patch('accounts.selectors.read_now', return_value=after_validity):
            result = self.search(self.assignment.course.name)
        self.assertTrue(result['partial'])
        self.assertEqual(next(source['state'] for source in result['sources'] if source['key'] == 'academic'), 'unavailable')
        self.assertFalse(any(item['category'] == 'Дисциплина' for item in result['items']))
        self.assertIn('/requests/new/?type=study_certificate', [item['url'] for item in self.search('справка')['items']])

    def test_revoked_session_old_marker_and_csrf_reject_queries(self):
        actor = current_actor(self.request())
        self.assertEqual(self.a.get('/search/results/').status_code, 405)
        self.assertEqual(self.a.post('/search/results/', {'q': 'справка', 'context': actor.marker},
            HTTP_X_COMPONENT='1', HTTP_X_PAGE_CONTEXT=actor.marker).status_code, 403)
        response = self.a.post('/search/results/', {'q': 'справка', 'context': 'old',
            'csrfmiddlewaretoken': self.a.cookies['csrftoken'].value}, HTTP_X_COMPONENT='1', HTTP_X_PAGE_CONTEXT='old')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'state': 'stale_context'})
        SessionContext.objects.filter(pk=actor.context_id).update(revoked_at=timezone.now())
        response = self.a.post('/search/results/', {'q': 'справка', 'context': actor.marker,
            'csrfmiddlewaretoken': self.a.cookies['csrftoken'].value}, HTTP_X_COMPONENT='1', HTTP_X_PAGE_CONTEXT=actor.marker)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn('items', response.json())

    def test_bounded_page_stability_and_generic_private_notes(self):
        for number in range(12):
            self.publish(title=f'Семинар поиск {number:02d}')
        first = self.search('Семинар поиск')
        second = self.search('Семинар поиск', page='2')
        self.assertEqual(first['count'], 12)
        self.assertEqual(len(first['items']), 10)
        self.assertEqual(len(second['items']), 2)
        self.assertTrue(set(item['key'] for item in first['items']).isdisjoint(item['key'] for item in second['items']))
        self.assertEqual(first, self.search('Семинар поиск'))
        self.assertNotIn('_keywords', json.dumps(first))
