from accounts.private_files import demo_state_dir
from django.core.management.base import BaseCommand, CommandError
from accounts.provisioning import provision

class Command(BaseCommand):
    help = 'Явно создать отсутствующие demo identities после импорта S2; существующие не менять'
    def handle(self,*args,**options):
        try:
            created = provision(demo_state_dir() / 'demo-credentials.json')
        except (ValueError,OSError) as exc:
            raise CommandError(str(exc))
        self.stdout.write(f'Создано: {len(created)}. Файл демопаролей: {demo_state_dir() / "demo-credentials.json"}.')
