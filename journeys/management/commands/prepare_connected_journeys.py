import json
from accounts.private_files import demo_state_dir
from django.core.management.base import BaseCommand,CommandError
from django.db import connection,transaction
from accounts.access import AccessDenied
from materials.services import MaterialError
from consultations.demo import provision as prepare_consultations
from journeys.demo import provision as prepare_journeys

class Command(BaseCommand):
    help='Явно добавить вымышленную подготовку и консультации, сохранив прежние действия и даты.'
    def add_arguments(self,parser):
        parser.add_argument('--database',required=True)
        parser.add_argument('--operator',default='connected-demonstration')
    def handle(self,*args,**options):
        if options['database']!=connection.settings_dict['NAME']:raise CommandError('Имя --database не совпадает с настроенной БД.')
        path=demo_state_dir()/'demo-credentials.json'
        try:
            with transaction.atomic():
                consultations=prepare_consultations(path,operator=options['operator'])
                journeys=prepare_journeys(path,operator=options['operator'])
        except (ValueError,AccessDenied,MaterialError) as exc:raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps({'consultations':consultations,'journeys':journeys},ensure_ascii=False))
