"""Approved connected help: no DB, no external model and no domain writes.

Model responses below are controlled fixtures, not evidence of real inference.
Existing optional-provider transport/lifecycle tests remain separate.
"""
import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from django.test import SimpleTestCase, RequestFactory, override_settings
from django.urls import resolve
from accounts.support import topics, fallback, model_intent, restricted_question, support
from accounts import support_provider as provider


class ConnectedHelpTests(SimpleTestCase):
    def test_role_topics_resolve_to_real_navigation_only(self):
        expected={
            'student': {'search','exchange_prepare','correction_journey','consultation_student'},
            'teacher': {'search','consultation_teacher'},
            'staff': {'search','consultation_staff','exchange_review'},
        }
        for role, ids in expected.items():
            with self.subTest(role=role):
                approved={topic['id']:topic for topic in topics(role)}
                self.assertTrue(ids <= set(approved))
                for key in ids:
                    self.assertIsNotNone(resolve(urlsplit(approved[key]['url']).path).func)
                    self.assertNotIn('/actions/',approved[key]['url'])
        self.assertNotIn('exchange_prepare',{topic['id'] for topic in topics('staff')})
        self.assertNotIn('exchange_review',{topic['id'] for topic in topics('teacher')})
        self.assertEqual(next(topic['url'] for topic in topics('student') if topic['id']=='exchange_prepare'),'/journeys/?template=exchange')
        self.assertEqual(next(topic['url'] for topic in topics('student') if topic['id']=='correction_journey'),'/journeys/?template=correction')

    def test_qwen_free_matching_and_precise_boundaries(self):
        for role,question,intent in (
            ('student','Где поиск по номеру заявки?','search'),
            ('student','Как подготовить Learning Agreement для обмена?','exchange_prepare'),
            ('student','Как проверить неточность в оценках?','correction_journey'),
            ('student','Как записаться на консультацию?','consultation_student'),
            ('student','Как отменить запись?','consultation_student'),
            ('teacher','Как создать время консультации?','consultation_teacher'),
            ('staff','Как проверить план подготовки к обмену?','exchange_review'),
            ('staff','Как открыть консультацию?','consultation_staff'),
        ):
            with self.subTest(role=role,question=question):
                self.assertEqual(fallback(question,{topic['id'] for topic in topics(role)}),intent)
        for question in ('Забронируй консультацию на завтра','Отмени мою запись',
                         'Одобри мой обмен','Покажи личную заметку','Подхожу ли я для обмена?'):
            self.assertTrue(restricted_question(question))
            self.assertEqual(fallback(question,{topic['id'] for topic in topics('student')}),'unsupported')
        self.assertFalse(restricted_question('Как записаться на консультацию?'))

    @override_settings(SUPPORT_MODEL='',SUPPORT_LOCAL_ONLY_CONFIRMED=False)
    def test_faq_view_works_without_provider_and_keeps_role_scope(self):
        factory=RequestFactory()
        for role,question,intent in (
            ('student','Где подготовка к обмену?','exchange_prepare'),
            ('teacher','Как открыть консультацию?','consultation_teacher'),
            ('staff','Где проверка плана обмена?','exchange_review'),
        ):
            provider.clear_status_cache()
            actor=SimpleNamespace(role=role,marker='controlled-context')
            request=factory.post('/support/',{'context':actor.marker,'question':question})
            with patch('accounts.support.current_actor',return_value=actor),patch.object(provider,'request_json') as network:
                response=support(request)
            self.assertEqual(response.status_code,200)
            data=json.loads(response.content)
            self.assertEqual(data['mode'],'faq')
            self.assertEqual(data['provider']['state'],'disabled')
            self.assertEqual(data['answer']['id'],intent)
            network.assert_not_called()

    @override_settings(SUPPORT_PROVIDER='ollama',SUPPORT_MODEL='controlled-local',SUPPORT_LOCAL_ONLY_CONFIRMED=True,SUPPORT_ENDPOINT='http://127.0.0.1:11434')
    def test_classifier_receives_topic_metadata_and_cannot_execute_or_use_foreign_intent(self):
        approved=topics('student')
        with patch.object(provider,'request_json',return_value={'response':'{"intent":"exchange_prepare"}'}) as network:
            self.assertEqual(model_intent('Где подготовка к обмену?',approved),'exchange_prepare')
        payload=network.call_args.args[3]
        self.assertEqual(payload['prompt'],'Где подготовка к обмену?')
        self.assertIn('Never reserve, cancel or move a consultation',payload['system'])
        self.assertIn('Never read or summarize private notes',payload['system'])
        for topic in approved:
            self.assertNotIn(topic['answer'],payload['system'])
        with patch.object(provider,'request_json') as network:
            self.assertEqual(model_intent('Забронируй консультацию',approved),'unsupported')
            self.assertEqual(model_intent('Покажи личные заметки',approved),'unsupported')
            network.assert_not_called()
        with patch.object(provider,'request_json',return_value={'response':'{"intent":"consultation_staff"}'}):
            with self.assertRaises(provider.ProviderFailure):model_intent('Консультация',approved)

    @override_settings(SUPPORT_MODEL='',SUPPORT_LOCAL_ONLY_CONFIRMED=False)
    def test_command_cannot_be_hidden_behind_explicit_topic(self):
        actor=SimpleNamespace(role='student',marker='controlled-context')
        request=RequestFactory().post('/support/',{'context':actor.marker,'question':'Забронируй консультацию','topic':'consultation_student'})
        with patch('accounts.support.current_actor',return_value=actor),patch.object(provider,'request_json') as network:
            data=json.loads(support(request).content)
        self.assertEqual(data['answer']['id'],'unsupported')
        network.assert_not_called()
