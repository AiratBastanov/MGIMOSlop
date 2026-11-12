from django.db import migrations
from django.utils import timezone


def backfill(apps, schema_editor):
    Identity = apps.get_model('accounts', 'Identity')
    Notification = apps.get_model('notifications', 'Notification')
    Receipt = apps.get_model('notifications', 'NotificationReceipt')
    Enrollment = apps.get_model('studies', 'Enrollment')
    now = timezone.now()
    for identity in Identity.objects.filter(role='student').select_related('profile__group__programme'):
        profile = identity.profile
        for event in Notification.objects.all().iterator():
            permitted = False
            if event.schedule_change_id:
                permitted = event.group_id == profile.group_id and event.schedule_change.dataset_id == profile.dataset_id
            elif event.request_event_id:
                permitted = event.request_event.request.owner_id == profile.pk
            elif event.announcement_change_id:
                item = event.announcement_change.announcement
                permitted = item.dataset_id == profile.dataset_id and item.status != 'draft' and (
                    item.audience == 'all_students' or item.audience == 'student' and item.student_id == profile.pk or
                    item.audience == 'group' and item.group_id == profile.group_id or
                    item.audience == 'faculty' and item.faculty_id == profile.group.programme.faculty_id)
            elif event.final_result_change_id:
                permitted = event.final_result_change.result.enrollment.student_id == profile.pk
            elif event.requirement_change_id:
                permitted = event.requirement_change.requirement.enrollment.student_id == profile.pk
            elif event.assessment_change_id:
                item = event.assessment_change.assessment
                permitted = item.group_id == profile.group_id and Enrollment.objects.filter(student_id=profile.pk,
                    course_id=item.course_id, period_id=item.period_id, status='active', dataset_id=profile.dataset_id).exists()
            if permitted:
                Receipt.objects.get_or_create(user_id=identity.user_id, notification_id=event.pk,
                    defaults={'available_at': max(now, event.published_at)})


class Migration(migrations.Migration):
    dependencies = [('notifications', '0006_inboxclear_emailoutbox_notificationreceipt'),
        ('accounts', '0004_teacher_identity'), ('profiles', '0001_initial')]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
