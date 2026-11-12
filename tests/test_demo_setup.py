"""Fresh-target conflict and interrupted composition on the disposable test DB."""
import json
from io import StringIO
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase, override_settings

from accounts.models import AccessAudit
from data_import.models import Dataset
from data_import.provider import SyntheticDatasetProvider
from studies.models import FinalResult


class DemoSetupTests(TransactionTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=settings.BASE_DIR / '.local')
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / '.local').mkdir()
        self.credentials = self.root / '.local/demo-credentials.json'

    def prepare(self):
        call_command('prepare_demo', database=connection.settings_dict['NAME'], stdout=StringIO())

    def test_foreign_credentials_and_wrong_target_are_preserved(self):
        self.credentials.write_text('{"unrelated":"keep-private"}', encoding='utf-8')
        before = self.credentials.read_bytes()
        with override_settings(BASE_DIR=self.root):
            with self.assertRaisesMessage(CommandError, 'Файл демопаролей'):
                self.prepare()
            with self.assertRaisesMessage(CommandError, 'не совпадает'):
                call_command('prepare_demo', database='deanery_wrong', stdout=StringIO())
        self.assertEqual(before, self.credentials.read_bytes())
        self.assertFalse(AccessAudit.objects.exists())
        self.assertFalse(Dataset.objects.exists())

    def test_resume_after_interruption_preserves_created_passwords_and_then_noop(self):
        shutil.copytree(settings.BASE_DIR / 'fixtures', self.root / 'fixtures')
        original = SyntheticDatasetProvider.load

        def interrupted(instance, path):
            if str(path).endswith('r5.json'):
                raise CommandError('controlled installation interruption')
            return original(instance, path)

        with override_settings(BASE_DIR=self.root):
            with patch.object(SyntheticDatasetProvider, 'load', interrupted):
                with self.assertRaisesMessage(CommandError, 'controlled installation interruption'):
                    self.prepare()
            before = json.loads(self.credentials.read_text(encoding='utf-8'))
            self.assertEqual(Dataset.objects.get().current_revision, 4)
            self.prepare()
            after = json.loads(self.credentials.read_text(encoding='utf-8'))
            self.assertTrue(all(after[k] == value for k, value in before.items()))
            self.assertEqual(set(after), {'student_a', 'student_b', 'teacher_demo', 'staff_demo'})
            self.assertEqual(Dataset.objects.get().current_revision, 11)
            self.assertEqual(FinalResult.objects.filter(enrollment__student__source_id='SP-A').count(), 22)
            audits = AccessAudit.objects.count()
            self.prepare()
            self.assertEqual(AccessAudit.objects.count(), audits)
            self.assertEqual(json.loads(self.credentials.read_text(encoding='utf-8')), after)
