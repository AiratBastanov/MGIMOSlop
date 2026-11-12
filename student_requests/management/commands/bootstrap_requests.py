from django.core.management.base import BaseCommand, CommandError
from accounts.access import AccessDenied
from student_requests.services import bootstrap_examples, RequestError


class Command(BaseCommand):
    help = 'Явно создать отсутствующие синтетические примеры заявок; существующие не изменяются.'

    def add_arguments(self, parser):
        parser.add_argument('--operator', required=True)

    def handle(self, *args, **options):
        try:
            rows = bootstrap_examples(operator=options['operator'])
        except (ValueError, AccessDenied, RequestError) as exc:
            raise CommandError(str(exc)) from exc
        for row in rows:
            self.stdout.write(f"{row['reference']}: {'создан синтетический пример' if row['created'] else 'сохранена существующая заявка'}")
