from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ('notifications', '0004_remove_notification_notification_typed_target_and_more'),
        ('student_requests', '0001_initial'),
    ]

    operations = [
        migrations.RemoveConstraint(model_name='notification', name='notification_typed_target'),
        migrations.AddField(
            model_name='notification', name='request_event',
            field=models.OneToOneField(null=True, on_delete=django.db.models.deletion.PROTECT,
                                       to='student_requests.requestevent'),
        ),
        migrations.AddConstraint(
            model_name='notification',
            constraint=models.CheckConstraint(condition=(
                models.Q(schedule_change__isnull=False, group__isnull=False, announcement_change__isnull=True,
                         final_result_change__isnull=True, requirement_change__isnull=True, assessment_change__isnull=True, request_event__isnull=True) |
                models.Q(schedule_change__isnull=True, group__isnull=True, announcement_change__isnull=False,
                         final_result_change__isnull=True, requirement_change__isnull=True, assessment_change__isnull=True, request_event__isnull=True) |
                models.Q(schedule_change__isnull=True, group__isnull=True, announcement_change__isnull=True,
                         final_result_change__isnull=False, requirement_change__isnull=True, assessment_change__isnull=True, request_event__isnull=True) |
                models.Q(schedule_change__isnull=True, group__isnull=True, announcement_change__isnull=True,
                         final_result_change__isnull=True, requirement_change__isnull=False, assessment_change__isnull=True, request_event__isnull=True) |
                models.Q(schedule_change__isnull=True, group__isnull=True, announcement_change__isnull=True,
                         final_result_change__isnull=True, requirement_change__isnull=True, assessment_change__isnull=False, request_event__isnull=True) |
                models.Q(schedule_change__isnull=True, group__isnull=True, announcement_change__isnull=True,
                         final_result_change__isnull=True, requirement_change__isnull=True, assessment_change__isnull=True, request_event__isnull=False)
            ), name='notification_typed_target'),
        ),
    ]
