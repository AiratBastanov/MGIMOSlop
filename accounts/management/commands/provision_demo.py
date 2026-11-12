from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from accounts.provisioning import provision

class Command(BaseCommand):
    help = 'Явно создать отсутствующие demo identities после импорта S2; существующие не менять'
    def handle(self,*args,**options):
        try:
            created = provision(settings.BASE_DIR / '.local/demo-credentials.json')
        except (ValueError,OSError) as exc:
            raise CommandError(str(exc))
        self.stdout.write(f'Создано: {len(created)}. Пароли: .local/demo-credentials.json (не публиковать).')
