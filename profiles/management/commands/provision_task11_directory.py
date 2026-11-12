"""Preview first; explicit apply uses the existing versioned import service."""
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from data_import.contracts import ImportRejected
from data_import.services import apply, preview
from profiles.demo_directory import prepare_directory_batch, provision_directory


class Command(BaseCommand):
    help = 'Подготовить/явно добавить вымышленных одногруппников TASK11 без перезаписи существующих данных.'

    def add_arguments(self, parser):
        parser.add_argument('--operator', required=True)
        parser.add_argument('--output', help='Новый файл принятого/подготовленного import payload; существующий не перезаписывается.')
        parser.add_argument('--apply', action='store_true')
        parser.add_argument('--expected-revision', type=int)

    def handle(self, *args, **options):
        operator = options['operator']
        if not operator.strip() or len(operator) > 100:
            raise CommandError('Укажите оператора до 100 символов.')
        if options['apply'] and options['expected_revision'] is None:
            raise CommandError('--apply требует явный --expected-revision.')
        try:
            batch, head = prepare_directory_batch(expected_revision=options['expected_revision'])
            if batch:
                if not options['output']:
                    raise CommandError('Новые учебные профили требуют --output для сохранения reviewable payload.')
                result = preview(batch, expected_revision=head)
                output = Path(options['output'])
                output.parent.mkdir(parents=True, exist_ok=True)
                with output.open('x', encoding='utf-8') as stream:
                    json.dump(batch.payload, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
                self.stdout.write(json.dumps(result, ensure_ascii=False))
                if options['apply']:
                    result = apply(batch, expected_revision=head, operator=operator)
                    self.stdout.write(json.dumps(result, ensure_ascii=False))
            else:
                self.stdout.write(f'Все учебные профили TASK11 уже приняты; revision {head}, новый импорт не нужен.')
            if options['apply']:
                self.stdout.write(json.dumps(provision_directory(operator=operator), ensure_ascii=False))
            else:
                self.stdout.write('PREVIEW: база, identities, фото и ресурсы не изменены. Для записи требуется --apply.')
        except (ImportRejected, ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
