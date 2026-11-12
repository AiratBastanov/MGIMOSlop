"""Readiness must not accept an old studies-only schema as the complete product."""
from unittest.mock import patch
from django.db import connection, OperationalError
from django.db.migrations.recorder import MigrationRecorder
from django.test import TransactionTestCase


class ReadinessTests(TransactionTestCase):
    def test_current_schema_is_ready_without_academic_seed(self):
        response = self.client.get('/health/ready/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertContains(response, 'Полнота и актуальность учебных данных проверяются отдельно')

    def test_missing_s5_migration_is_not_ready(self):
        recorder = MigrationRecorder(connection)
        recorder.record_unapplied('student_requests', '0002_request_integrity')
        try:
            response = self.client.get('/health/ready/')
            self.assertEqual(response.status_code, 503)
            self.assertNotContains(response, 'Схема приложения готова', status_code=503)
        finally:
            recorder.record_applied('student_requests', '0002_request_integrity')

    def test_db_failure_has_no_private_configuration(self):
        with patch('deanery.views.MigrationExecutor', side_effect=OperationalError('private-db-password')):
            response = self.client.get('/health/ready/')
        self.assertEqual(response.status_code, 503)
        self.assertNotContains(response, 'private-db-password', status_code=503)
        self.assertIn('no-store', response['Cache-Control'])
