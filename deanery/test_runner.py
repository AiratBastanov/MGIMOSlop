from django.test.runner import DiscoverRunner
from django.core.management import call_command

class ExistingDatabaseRunner(DiscoverRunner):
    """Отдельная заранее созданная disposable БД; роль без CREATEDB."""
    def setup_databases(self, **kwargs):
        call_command('migrate', verbosity=0, interactive=False)
        call_command('flush', verbosity=0, interactive=False)
        return None

    def teardown_databases(self, old_config, **kwargs):
        call_command('flush', verbosity=0, interactive=False)
