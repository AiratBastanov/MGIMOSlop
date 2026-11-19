"""Явная идемпотентная подготовка вымышленного преподавателя и назначений."""
from datetime import datetime,time
from zoneinfo import ZoneInfo
from pathlib import Path
from accounts.private_files import demo_state_dir
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from accounts.access import lock_guard
from accounts.models import AccessAudit,Identity
from accounts.provisioning import provision_teacher
from studies.models import Lesson,Enrollment,StudentProfile
from materials.models import TeachingAssignment,SubmissionPolicy
from materials.selectors import CLOUD_HOSTS


class Command(BaseCommand):
    help='Добавить teacher_demo и отсутствующие учебные назначения, не меняя существующие настройки и действия.'

    def add_arguments(self,parser):
        parser.add_argument('--credentials',default=str(demo_state_dir()/'demo-credentials.json'))
        parser.add_argument('--operator',required=True)

    def handle(self,*args,**options):
        if not options['operator'].strip() or len(options['operator'])>100:
            raise CommandError('Укажите оператора до 100 символов.')
        profile=StudentProfile.objects.get(source_id='SP-A')
        enrollments=Enrollment.objects.filter(student=profile,status='active').values('course_id')
        existing=Identity.objects.filter(user__username='teacher_demo').first()
        if existing:
            instructor_id=existing.instructor_id
        else:
            lesson=Lesson.objects.filter(group=profile.group,course_id__in=enrollments,instructor__isnull=False).order_by('pk').first()
            if lesson is None:
                raise CommandError('Нет подготовленного преподавателя в занятиях student_a.')
            instructor_id=lesson.instructor_id
        identity,created=provision_teacher(options['credentials'],instructor_id)
        assignments=policies=0
        with transaction.atomic():
            guard=lock_guard()
            lessons=Lesson.objects.filter(instructor_id=instructor_id).select_related('course','group','period').order_by('pk')
            seen=set()
            for lesson in lessons:
                key=(lesson.course_id,lesson.group_id,lesson.period_id)
                if key in seen: continue
                seen.add(key)
                if not Enrollment.objects.filter(student__group_id=lesson.group_id,course_id=lesson.course_id,
                        period_id=lesson.period_id,status='active').exists(): continue
                row,new=TeachingAssignment.objects.get_or_create(teacher=identity,course=lesson.course,group=lesson.group,
                    period=lesson.period,defaults={
                        'available_from':datetime.combine(lesson.period.date_from,time.min,ZoneInfo('Europe/Moscow')),
                        'available_until':datetime.combine(lesson.period.date_until,time.min,ZoneInfo('Europe/Moscow'))})
                assignments+=int(new)
                if new:
                    SubmissionPolicy.objects.create(assignment=row,version=1,modes=['text','link','email'],
                        text_limit=5000,cloud_hosts=list(CLOUD_HOSTS),
                        instructions='Синтетический учебный пример. Отправьте текст, облачную ссылку или контактную почту.',author_id=identity.user_id)
                    policies+=1
            if assignments:
                guard.revision+=1; guard.save(update_fields=['revision'])
                AccessAudit.objects.create(user_id=identity.user_id,action='teaching_provision',revision=guard.revision,operator=options['operator'])
        self.stdout.write(f'teacher_demo: {"создан" if created else "сохранён"}; новых назначений: {assignments}; новых условий: {policies}. Пароли не выводятся.')
