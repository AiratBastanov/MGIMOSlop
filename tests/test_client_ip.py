"""S7: single-proxy attribution and the real Axes key in the isolated test DB."""
from datetime import timedelta
from unittest.mock import patch

from axes.models import AccessAttempt
from django.test import Client, RequestFactory, SimpleTestCase, TransactionTestCase, override_settings
from django.utils import timezone

from accounts.access import client_ip
from tests import test_auth


class ClientIPTests(SimpleTestCase):
    def resolve(self, remote='127.0.0.1', header='198.51.100.7'):
        return client_ip(RequestFactory().get('/', REMOTE_ADDR=remote,
            HTTP_X_DEANERY_CLIENT_IP=header, HTTP_X_FORWARDED_FOR='203.0.113.8',
            HTTP_FORWARDED='for=203.0.113.9', HTTP_X_REAL_IP='203.0.113.10'))

    @override_settings(CLIENT_IP_TRUSTED_PROXY='')
    def test_direct_mode_ignores_every_forwarded_header(self):
        for remote in ('127.0.0.1', '192.0.2.20', '::1', '', None):
            self.assertEqual(self.resolve(remote), remote)

    @override_settings(CLIENT_IP_TRUSTED_PROXY='127.0.0.1')
    def test_only_exact_immediate_proxy_is_trusted(self):
        for remote in ('127.0.0.2', '10.0.0.1', '192.168.0.1', '::1', '::ffff:127.0.0.1', None):
            self.assertEqual(self.resolve(remote), remote)

    @override_settings(CLIENT_IP_TRUSTED_PROXY='127.0.0.1')
    def test_one_ipv4_or_ipv6_address_is_canonicalized(self):
        self.assertEqual(self.resolve(), '198.51.100.7')
        self.assertEqual(self.resolve(header='2001:0DB8:0:0:0:0:0:7'), '2001:db8::7')

    @override_settings(CLIENT_IP_TRUSTED_PROXY='127.0.0.1')
    def test_absent_malformed_and_chain_fall_back_to_proxy_bucket(self):
        request = RequestFactory().get('/', REMOTE_ADDR='127.0.0.1')
        self.assertEqual(client_ip(request), '127.0.0.1')
        for value in ('', None, 'unknown', '198.51.100.7, 192.0.2.1',
                      '198.51.100.7:443', '[2001:db8::7]', 'fe80::1%eth0',
                      ' 198.51.100.7', '198.51.100.7 ', '198.051.100.7', '2130706433'):
            self.assertEqual(self.resolve(header=value), '127.0.0.1', repr(value))


@override_settings(CLIENT_IP_TRUSTED_PROXY='127.0.0.1')
class ProxyAxesTests(TransactionTestCase):
    # Existing isolated provisioning; never reads judge credentials or working DB.
    setUp = test_auth.AuthTests.setUp

    def test_same_username_has_separate_resolved_buckets_and_original_lifecycle(self):
        client = Client(enforce_csrf_checks=True)
        client.get('/login/')
        start = timezone.now()

        def attempt(ip='198.51.100.7', password='wrong', ua='one'):
            return client.post('/login/', {'username': 'student_a', 'password': password,
                'csrfmiddlewaretoken': client.cookies['csrftoken'].value},
                REMOTE_ADDR='127.0.0.1', HTTP_X_DEANERY_CLIENT_IP=ip,
                HTTP_USER_AGENT=ua, HTTP_X_FORWARDED_FOR='203.0.113.99')

        with patch('django.utils.timezone.now', return_value=start), patch('axes.handlers.proxy.now', return_value=start):
            for i in range(4):
                self.assertEqual(attempt(ua=str(i % 2)).status_code, 200)
            self.assertEqual(attempt().status_code, 429)
            self.assertEqual(attempt(ip='2001:db8::8').status_code, 200)
        self.assertEqual(set(AccessAttempt.objects.values_list('ip_address', flat=True)),
                         {'198.51.100.7', '2001:db8::8'})
        with patch('django.utils.timezone.now', return_value=start), patch('axes.handlers.proxy.now', return_value=start):
            self.assertEqual(attempt(ip='2001:db8::8', password=self.credentials['student_a']).status_code, 302)
        self.assertFalse(AccessAttempt.objects.filter(ip_address='2001:db8::8').exists())
        self.assertTrue(AccessAttempt.objects.filter(ip_address='198.51.100.7').exists())
        # New unauthenticated session; the username/IP bucket remains in the DB.
        client = Client(enforce_csrf_checks=True)
        client.get('/login/')
        rows = list(AccessAttempt.objects.values('attempt_time', 'failures_since_start'))
        for delta in (timedelta(minutes=14), timedelta(minutes=15)):
            with patch('django.utils.timezone.now', return_value=start + delta), patch('axes.handlers.proxy.now', return_value=start + delta):
                self.assertEqual(attempt(password=self.credentials['student_a'], ua='new').status_code, 429)
            if delta == timedelta(minutes=14):
                self.assertEqual(list(AccessAttempt.objects.values('attempt_time', 'failures_since_start')), rows)
        later = start + timedelta(minutes=15, microseconds=1)
        with patch('django.utils.timezone.now', return_value=later), patch('axes.handlers.proxy.now', return_value=later):
            self.assertEqual(attempt(password=self.credentials['student_a']).status_code, 302)
        self.assertFalse(AccessAttempt.objects.filter(username='student_a', ip_address='198.51.100.7').exists())
