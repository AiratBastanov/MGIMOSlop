from datetime import datetime, time
from zoneinfo import ZoneInfo
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from accounts.access import lock_guard
from accounts.models import Identity, StaffScope
from studies.models import StudentProfile, Period
from profiles.models import GroupResource, GroupResourceRevision


class Command(BaseCommand):
    help = 'Добавить отсутствующие иллюстративные ресурсы МБ-21, сохраняя изменения сотрудника.'

    def add_arguments(self, parser):
        parser.add_argument('--operator', required=True)

    def handle(self, *args, **options):
        if not options['operator'].strip():
            raise CommandError('Укажите оператора.')
        with transaction.atomic():
            lock_guard()
            identity = Identity.objects.get(user__username='staff_demo', role='staff')
            profile = StudentProfile.objects.select_related('group__programme').get(source_id='SP-A')
            if not StaffScope.objects.filter(identity=identity, faculty_id=profile.group.programme.faculty_id).exists():
                raise CommandError('Нет разрешённого издателя для группы.')
            period = Period.objects.filter(dataset=profile.dataset).order_by('-date_until').first()
            end = datetime.combine(period.date_until, time.min, ZoneInfo('Europe/Moscow'))
            start = datetime.combine(period.date_from, time.min, ZoneInfo('Europe/Moscow'))
            examples = [('internship','Пример практики: исследование рынка','Иллюстративное задание для группы: подготовить обзор рынка и краткий отчёт. Это вымышленный пример, а не открытая стажировка или предложение работодателя.'),
                ('course','Пример дополнительного курса: работа с данными','Иллюстративная программа: таблицы, проверка источников, визуализация. Реальный набор и партнёр университета не заявлены.'),
                ('document','Памятка группы: как подготовить материал','Создайте черновик в разделе «Учёба / Материалы», проверьте правила преподавателя и отправьте версию. Отзыв на материал не является официальной оценкой.')]
            added = 0
            for category, title, description in examples:
                row, created = GroupResource.objects.get_or_create(demo_key='task10-mb21-'+category,
                    defaults={'group':profile.group,'publisher':identity,'category':category,'title':title,
                        'description':description,'available_from':start,'available_until':end})
                if created:
                    added += 1
                    GroupResourceRevision.objects.create(resource=row,version=1,actor=identity,payload={
                        'title':title,'description':description,'category':category,'group_id':profile.group_id,
                        'available_from':start.isoformat(),'available_until':end.isoformat(),'operator':options['operator'],'synthetic':True})
        self.stdout.write(f'Новых иллюстративных ресурсов: {added}; существующие публикации сохранены.')
