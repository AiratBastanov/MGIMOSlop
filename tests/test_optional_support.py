"""Controlled loopback providers; these tests do not claim real model inference."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
from unittest.mock import patch

from django.test import SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts import support_provider as provider
from accounts.models import SessionContext
from accounts.support import model_intent, topics
from tests import test_auth

MODEL = 'qwen3:4b-instruct-2507-q4_K_M'


class LocalProvider:
    """A deterministic HTTP fixture, bound only to an ephemeral loopback port."""
    def __init__(self):
        self.model_names = [MODEL]
        self.status = 200
        self.output = {'response': json.dumps({'intent': 'grades'})}
        self.delay = 0
        self.paths = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, data, status=200):
                raw = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_GET(self):
                fixture.paths.append(self.path)
                if self.path == '/api/ps':
                    self.respond({'models': []})
                else:
                    self.respond({'models': [{'name': name} for name in fixture.model_names]})

            def do_POST(self):
                fixture.paths.append(self.path)
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                time.sleep(fixture.delay)
                self.respond(fixture.output, fixture.status)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.endpoint = f'http://127.0.0.1:{self.server.server_port}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@override_settings(SUPPORT_PROVIDER='ollama', SUPPORT_MODEL=MODEL, SUPPORT_LOCAL_ONLY_CONFIRMED=True)
class ProviderTests(SimpleTestCase):
    def setUp(self):
        provider.clear_status_cache()

    def test_disabled_and_configuration_boundary_without_network(self):
        with patch.object(provider, 'request_json') as http:
            with override_settings(SUPPORT_PROVIDER=''):
                self.assertEqual(provider.availability(probe=True)['state'], 'disabled')
            for endpoint in ('https://127.0.0.1', 'http://localhost:11434', 'http://192.0.2.1',
                             'http://user:secret@127.0.0.1', 'http://127.0.0.1/api', 'http://127.0.0.1?x=1'):
                with override_settings(SUPPORT_ENDPOINT=endpoint):
                    self.assertEqual(provider.availability(probe=True)['reason'], 'invalid_configuration')
            with override_settings(SUPPORT_MODEL='qwen-cloud'):
                self.assertEqual(provider.availability(probe=True)['state'], 'disabled')
            http.assert_not_called()

    def test_actual_no_listener_is_bounded_and_cached(self):
        # Reserve a socket without listening so this cannot contact a shared service.
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1', 0))
            with override_settings(SUPPORT_ENDPOINT=f'http://127.0.0.1:{reserved.getsockname()[1]}'):
                start = time.monotonic()
                self.assertEqual(provider.availability()['state'], 'checking')
                self.assertEqual(provider.availability(probe=True)['state'], 'unavailable')
                self.assertLess(time.monotonic() - start, 3)
                with patch.object(provider, 'request_json') as request:
                    self.assertEqual(provider.availability(probe=True)['state'], 'unavailable')
                    request.assert_not_called()

    def test_exact_tag_unloaded_model_cache_and_manual_recovery(self):
        with LocalProvider() as fixture, override_settings(SUPPORT_ENDPOINT=fixture.endpoint):
            fixture.model_names = ['qwen3:latest']
            self.assertEqual(provider.availability(probe=True)['state'], 'model_missing')
            fixture.model_names = [MODEL]
            self.assertEqual(provider.availability(probe=True, refresh=True)['state'], 'model_missing')
            with patch.object(provider, 'RECHECK_COOLDOWN', 0):
                self.assertEqual(provider.availability(probe=True, refresh=True)['state'], 'available')
            self.assertEqual(model_intent('оценки', topics('student')), 'grades')
            self.assertNotIn('/api/ps', fixture.paths)  # Loaded-memory state never determines installation.

    def test_inference_timeout_errors_malformed_and_disappearing_provider(self):
        with LocalProvider() as fixture, override_settings(SUPPORT_ENDPOINT=fixture.endpoint):
            self.assertEqual(provider.availability(probe=True)['state'], 'available')
            for output in (b'not json', [], {'response': '{}'}, {'response': '{"intent":"grades","url":"evil"}'},
                           {'response': '{"intent":["grades"]}'}, {'response': 3}, {'error': 'private raw error'},
                           b'x' * (provider.MAX_BYTES + 2), b'[' * 1500 + b']' * 1500,
                           {'response': '[' * 1500 + ']' * 1500}):
                fixture.output = output
                with self.assertRaises(provider.ProviderFailure):
                    model_intent('оценки', topics('student'))
            fixture.output = {'response': '{"intent":"grades"}'}
            for status in (404, 429, 500):
                fixture.status = status
                with self.assertRaises(provider.ProviderFailure):
                    model_intent('оценки', topics('student'))
            fixture.status, fixture.delay = 200, .3
            with patch.object(provider, 'INFERENCE_TIMEOUT', .08):
                start = time.monotonic()
                with self.assertRaises(provider.ProviderFailure) as caught:
                    model_intent('оценки', topics('student'))
                self.assertEqual(caught.exception.reason, 'timeout')
                self.assertLess(time.monotonic() - start, .7)
        with override_settings(SUPPORT_ENDPOINT=fixture.endpoint):
            with self.assertRaises(provider.ProviderFailure):
                model_intent('оценки', topics('student'))

    def test_invalid_status_schema_and_compatible_local_provider(self):
        for bad in ({}, {'models': 'bad'}, {'models': [None]}, {'models': [{'name': 3}]}):
            provider.clear_status_cache()
            with patch.object(provider, 'request_json', return_value=bad):
                self.assertEqual(provider.availability(probe=True)['reason'], 'invalid_response')
        provider.clear_status_cache()
        with override_settings(SUPPORT_PROVIDER='openai_local'), patch.object(provider, 'request_json', return_value={'data': [{'id': MODEL}]}):
            self.assertEqual(provider.availability(probe=True)['state'], 'available')


@override_settings(SUPPORT_PROVIDER='ollama', SUPPORT_MODEL=MODEL, SUPPORT_LOCAL_ONLY_CONFIRMED=True)
class SupportViewTests(TransactionTestCase):
    setUp = test_auth.AuthTests.setUp
    login = test_auth.AuthTests.login
    page = test_auth.AuthTests.page
    marker = test_auth.AuthTests.marker

    def ask(self, **extra):
        return self.a.post('/support/', {'context': self.marker(), 'question': 'Где оценки?',
            'csrfmiddlewaretoken': self.a.cookies['csrftoken'].value, **extra}, HTTP_X_COMPONENT='1')

    def test_faq_model_provenance_and_failure_containment(self):
        self.login()
        provider.clear_status_cache()
        with LocalProvider() as fixture, override_settings(SUPPORT_ENDPOINT=fixture.endpoint):
            current = self.marker()
            initial = self.a.get('/support/', HTTP_X_PAGE_CONTEXT=current).json()
            self.assertTrue(initial['topics'])
            self.assertEqual(initial['provider']['state'], 'checking')
            self.assertEqual(fixture.paths, [])
            self.a.get('/support/?check=1', HTTP_X_PAGE_CONTEXT=current)
            self.assertEqual(self.ask(topic='grades').json()['mode'], 'faq')
            self.assertNotIn('/api/generate', fixture.paths)
            self.assertEqual(self.ask().json()['mode'], 'model')
            fixture.output = {'response': 'invalid'}
            fallback = self.ask().json()
            self.assertEqual(fallback['mode'], 'faq')
            self.assertEqual(fallback['answer']['id'], 'grades')
            self.assertEqual(fallback['provider']['state'], 'inference_failed')
            with patch.object(provider, 'request_json') as request:
                self.assertEqual(self.ask(topic='grades').json()['mode'], 'faq')
                request.assert_not_called()

    def test_delayed_status_and_answer_recheck_current_context(self):
        for check in (True, False):
            self.login()
            marker = self.marker()
            provider.clear_status_cache()

            def delayed(*args, **kwargs):
                SessionContext.objects.filter(user__username='student_a', revoked_at__isnull=True).update(revoked_at=timezone.now())
                return {'models': [{'name': MODEL}]} if check else {'response': '{"intent":"grades"}'}

            if not check:
                with patch.object(provider, 'request_json', return_value={'models': [{'name': MODEL}]}):
                    provider.availability(probe=True)
            with patch.object(provider, 'request_json', side_effect=delayed):
                if check:
                    response = self.a.get('/support/?check=1', HTTP_X_PAGE_CONTEXT=marker, HTTP_X_COMPONENT='1')
                else:
                    response = self.ask()
            self.assertIn(response.status_code, (401, 403, 409))
            self.assertNotIn('answer', response.json())
            self.assertNotIn('topics', response.json())

    def test_authentication_csrf_and_programming_errors_are_not_provider_failures(self):
        with patch.object(provider, 'request_json') as request:
            self.assertEqual(self.a.get('/support/').status_code, 302)
            self.login()
            self.assertEqual(self.a.post('/support/', {'question': 'оценки'}).status_code, 403)
            request.assert_not_called()
        with patch.object(provider, 'availability', side_effect=RuntimeError('programming error')):
            with self.assertRaises(RuntimeError):
                self.a.get('/support/', HTTP_X_PAGE_CONTEXT=self.marker())
