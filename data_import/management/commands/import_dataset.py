import json
from django.db import DatabaseError
from django.core.management.base import BaseCommand, CommandError
from data_import.contracts import ImportRejected, SourceUnavailable
from data_import.provider import SyntheticDatasetProvider
from data_import.services import preview, apply
from data_import.models import ImportAttempt

class Command(BaseCommand):
    help = 'Проверить файл/diff; --apply явно сохраняет прошедший повторную проверку пакет'
    def add_arguments(self, parser):
        parser.add_argument('path')
        parser.add_argument('--expected-revision',type=int,required=True)
        parser.add_argument('--apply',action='store_true')
        parser.add_argument('--operator')

    def handle(self, **options):
        if options['apply'] and not options['operator']:
            raise CommandError('--apply требует --operator')
        try:
            batch = SyntheticDatasetProvider().load(options['path'])
            result = apply(batch,expected_revision=options['expected_revision'],operator=options['operator']) if options['apply'] else preview(batch,expected_revision=options['expected_revision'])
            self.stdout.write(json.dumps(result,ensure_ascii=False,indent=2))
        except ImportRejected as exc:
            if options['apply']:
                try:
                    ImportAttempt.objects.create(outcome='source_unavailable' if isinstance(exc,SourceUnavailable) else 'invalid_update',code=exc.code)
                except DatabaseError:
                    pass
            raise CommandError(f'{exc.code}: {exc}') from exc
        except DatabaseError as exc:
            raise CommandError('database_unavailable: запись не подтверждена; повторите проверку текущей revision') from exc
