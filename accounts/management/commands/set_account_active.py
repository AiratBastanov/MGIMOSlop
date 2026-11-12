from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from accounts.access import set_active

class Command(BaseCommand):
    help = 'Операторское включение/отключение доступа с аннулированием контекстов'
    def add_arguments(self,parser):
        parser.add_argument('username')
        parser.add_argument('state',choices=['active','inactive'])
        parser.add_argument('--operator',required=True)
    def handle(self,*args,**options):
        user = get_user_model().objects.filter(username=options['username']).first()
        if not user: raise CommandError('Неизвестная учётная запись')
        changed = set_active(user.pk,options['state']=='active',operator=options['operator'])
        self.stdout.write('Доступ изменён.' if changed else 'Изменение не требуется.')
