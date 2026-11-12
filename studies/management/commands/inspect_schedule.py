import json
from datetime import date
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from studies.selectors import operator_schedule, operator_snapshot

class Command(BaseCommand):
    help = 'Локальное операторское чтение; не является публичным API'
    def add_arguments(self, parser):
        parser.add_argument('--date',type=date.fromisoformat,default=date(2026,11,23))
        parser.add_argument('--group',default='G-S1')
        parser.add_argument('--period',default='P-2026-autumn')
        parser.add_argument('--snapshot',action='store_true')
    def handle(self, **options):
        try:
            result = operator_snapshot() if options['snapshot'] else operator_schedule(group_id=options['group'],period_id=options['period'],date=options['date'])
        except DatabaseError as exc:
            raise CommandError('database_unavailable: чтение не выполнено') from exc
        self.stdout.write(json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2))
