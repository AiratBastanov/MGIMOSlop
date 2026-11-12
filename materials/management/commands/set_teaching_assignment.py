from django.core.management.base import BaseCommand
from materials.services import set_assignment_active


class Command(BaseCommand):
    help='Явно включить/отключить назначение преподавателя с аннулированием старого контекста доступа.'

    def add_arguments(self,parser):
        parser.add_argument('assignment_id',type=int)
        parser.add_argument('--active',choices=['yes','no'],required=True)
        parser.add_argument('--operator',required=True)

    def handle(self,*args,**options):
        changed=set_assignment_active(options['assignment_id'],options['active']=='yes',operator=options['operator'])
        self.stdout.write('Назначение изменено.' if changed else 'Назначение уже в указанном состоянии.')
