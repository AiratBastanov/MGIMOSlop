from accounts.private_files import demo_state_dir
from django.core.management.base import BaseCommand,CommandError
from workspaces.demo import provision
import json

class Command(BaseCommand):
    help='Явно добавить синтетический набор TASK16 через сервисы ролей; повтор ничего не сбрасывает.'
    def add_arguments(self,parser):
        parser.add_argument('--credentials',default=str(demo_state_dir()/'demo-credentials.json'))
        parser.add_argument('--operator',required=True)
    def handle(self,*args,**options):
        try:result=provision(options['credentials'],operator=options['operator'])
        except (ValueError,OSError) as exc:raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result,ensure_ascii=False))
