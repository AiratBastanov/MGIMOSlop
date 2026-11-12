import json
from django.core.management.base import BaseCommand,CommandError
from profiles.demo_task14 import provision_contacts

class Command(BaseCommand):
    help='Preview/инициализация трёх определённых вымышленных контактов; локальное capture, без отправки почты.'
    def add_arguments(self,parser):
        parser.add_argument('--operator',required=True)
        parser.add_argument('--apply',action='store_true')
    def handle(self,**options):
        try:self.stdout.write(json.dumps(provision_contacts(operator=options['operator'],apply=options['apply']),ensure_ascii=False,indent=2))
        except ValueError as exc:raise CommandError(str(exc)) from exc
