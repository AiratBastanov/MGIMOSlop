import json
from django.core.management.base import BaseCommand
from notifications.delivery import deliver_pending


class Command(BaseCommand):
    help = 'Обработать очередь писем; по умолчанию только защищённый локальный capture.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **options):
        self.stdout.write(json.dumps(deliver_pending(limit=max(1, min(1000, options['limit']))), ensure_ascii=False))
