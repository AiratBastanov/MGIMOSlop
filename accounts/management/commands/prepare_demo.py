"""One explicit, resumable composition; never used by login, startup or GET."""
import json
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from accounts.access import lock_guard
from accounts.models import AccessAudit, AuthorizationGuard
from accounts.private_files import protect_directory
from accounts.provisioning import provision
from data_import.contracts import ImportRejected
from data_import.models import Dataset, ImportRevision
from data_import.provider import SyntheticDatasetProvider
from data_import.services import apply, check_integrity, get_head, initialize, preview
from profiles.demo_directory import provision_directory
from profiles.demo_task14 import provision_contacts
from student_requests.services import bootstrap_examples
from workspaces.demo import provision as provision_workspaces

EDITION = 'review-demo-v1'
LOCK = 749240117
SOURCES = ('s1/r1.json', 's1/r2.json', 's2/r3.json', 's2/r4.json',
           's3/r5.json', 's3/r6.json', 's3/r7.json', 's4/r8.json', 's4/r9.json',
           'demo/directory.json', 'task14/r11.json')
DOMAIN_APPS = ('accounts', 'data_import', 'studies', 'student_requests', 'notifications', 'materials', 'profiles', 'workspaces')


class Command(BaseCommand):
    help = 'Подготовить полное вымышленное демо в пустой БД; продолжить прерванную подготовку или сохранить готовое демо.'

    def add_arguments(self, parser):
        parser.add_argument('--database', required=True, help='Точное имя БД из конфигурации; защита от неверной цели.')

    def handle(self, *args, **options):
        target = connection.settings_dict['NAME']
        if options['database'] != target:
            raise CommandError('Имя --database не совпадает с настроенной БД. Ничего не изменено.')
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', [LOCK])
            locked = cursor.fetchone()[0]
        if not locked:
            raise CommandError('В этой БД уже идёт подготовка демо. Дождитесь её завершения.')
        try:
            self.prepare()
        except (ImportRejected, ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        finally:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_unlock(%s)', [LOCK])

    def prepare(self):
        started = AccessAudit.objects.filter(action='demo_composition_started', operator=EDITION).exists()
        if not started:
            if (settings.BASE_DIR / '.local/demo-credentials.json').exists():
                raise CommandError('Файл демопаролей уже принадлежит другой установке. Используйте отдельную копию проекта; файл сохранён.')
            # Migrations create framework permissions; every application table must be empty.
            from django.contrib.auth import get_user_model
            if (get_user_model().objects.exists()
                    or AuthorizationGuard.objects.exclude(pk=1, revision=1).exists()
                    or any(model.objects.exists() for label in DOMAIN_APPS
                           for model in apps.get_app_config(label).get_models() if model is not AuthorizationGuard)):
                raise CommandError('Нужна пустая БД после migrate или незавершённая установка этой композиции. Существующие данные сохранены.')
            protect_directory(settings.BASE_DIR / '.local')
            with transaction.atomic():
                guard, _ = AuthorizationGuard.objects.get_or_create(pk=1)
                lock_guard()
                AccessAudit.objects.create(action='demo_composition_started', operator=EDITION, revision=guard.revision)
        if AccessAudit.objects.filter(action='demo_composition_complete', operator=EDITION).exists():
            head = get_head()
            check_integrity(head)
            if head.current_revision < 11:
                raise CommandError('Нарушена целостность композиции. Автоматическое восстановление запрещено.')
            self.stdout.write('Демонстрация уже подготовлена. Данные, пароли и пользовательские действия сохранены.')
            return

        credentials = settings.BASE_DIR / '.local/demo-credentials.json'
        protect_directory(credentials.parent)
        if Dataset.objects.exists() and get_head().current_revision > 11:
            raise CommandError('Источник продвинулся во время незавершённой установки. Старые примеры не применены.')
        initialize()
        provider = SyntheticDatasetProvider()
        for revision, source in enumerate(SOURCES, 1):
            batch = provider.load(settings.BASE_DIR / 'fixtures' / source)
            head = get_head()
            if head.current_revision >= revision:
                if not ImportRevision.objects.filter(dataset=head, revision=revision, content_hash=batch.payload['content_sha256']).exists():
                    raise CommandError('Принята другая версия учебного набора. Подготовка остановлена без перезаписи.')
            else:
                preview(batch, expected_revision=revision - 1)
                apply(batch, expected_revision=revision - 1, operator=EDITION)
            if revision == 4:
                provision(credentials)
        if get_head().current_revision != 11:
            raise CommandError('Источник продвинулся во время незавершённой установки. Старые примеры не применены.')
        call_command('provision_task10', credentials=str(credentials), operator=EDITION, stdout=self.stdout)
        bootstrap_examples(operator=EDITION)
        call_command('provision_group_resources', operator=EDITION, stdout=self.stdout)
        provision_directory(operator=EDITION)
        capture = protect_directory(settings.BASE_DIR / '.local/demo-mail') / 'email'
        provision_contacts(operator=EDITION, apply=True, capture_dir=capture)
        result = provision_workspaces(credentials, operator=EDITION, edition=EDITION)
        with transaction.atomic():
            guard = lock_guard()
            AccessAudit.objects.create(action='demo_composition_complete', operator=EDITION, revision=guard.revision)
        self.stdout.write('Подготовлены два студента, преподаватель, служба, учебные факты, группа, приватные файлы, задания и уточнение заявки.')
        self.stdout.write('Локальные пароли: .local/demo-credentials.json. Не включайте файл в репозиторий.')
