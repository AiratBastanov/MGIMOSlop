import json
from django.core.management.base import BaseCommand, CommandError
from data_import.services import initialize
from data_import.contracts import ImportRejected

class Command(BaseCommand):
    help = 'Явно создать только пустой import head; повтор не сбрасывает данные'
    def handle(self, **options):
        try:
            self.stdout.write(json.dumps(initialize(),ensure_ascii=False))
        except ImportRejected as exc:
            raise CommandError(f'{exc.code}: {exc}') from exc
