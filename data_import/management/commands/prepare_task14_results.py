import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from data_import.demo_task14 import prepare_results
from data_import.services import preview
from data_import.contracts import ImportRejected

class Command(BaseCommand):
    help='Подготовить и проверить добавления TASK14; запись только отдельной командой import_dataset.'
    def add_arguments(self,parser):
        parser.add_argument('--expected-revision',type=int,required=True)
        parser.add_argument('--output',required=True)
    def handle(self,**options):
        try:
            batch=prepare_results(options['expected_revision'])
            if batch is None:
                self.stdout.write('NOOP: все стабильные записи TASK14 уже присутствуют.');return
            result=preview(batch,expected_revision=options['expected_revision'])
            path=Path(options['output']);path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('x',encoding='utf-8') as f:json.dump(batch.payload,f,ensure_ascii=False,indent=2)
            self.stdout.write(json.dumps({'outcome':result['outcome'],'revision':result['revision'],'operations':len(batch.operations),'path':str(path)},ensure_ascii=False))
        except (ValueError,OSError,ImportRejected) as exc:raise CommandError(str(exc)) from exc
