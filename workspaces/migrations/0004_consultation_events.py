from django.db import migrations, models
import django.db.models.deletion

SQL="""
ALTER TABLE workspaces_applicationevent DROP CONSTRAINT application_event_exact_target;
ALTER TABLE workspaces_applicationevent ADD CONSTRAINT application_event_exact_target CHECK (
 num_nonnulls(teaching_revision_id,submission_id,review_id,clarification_message_id,notice_revision_id,resource_revision_id,consultation_event_id)=1
 AND ((kind='teaching' AND teaching_revision_id IS NOT NULL) OR (kind='submission' AND submission_id IS NOT NULL)
 OR (kind='review' AND review_id IS NOT NULL) OR (kind='clarification' AND clarification_message_id IS NOT NULL)
 OR (kind='notice' AND notice_revision_id IS NOT NULL) OR (kind='resource' AND resource_revision_id IS NOT NULL)
 OR (kind='consultation' AND consultation_event_id IS NOT NULL)));
"""

class Migration(migrations.Migration):
    dependencies=[('workspaces','0003_required_events'),('consultations','0001_initial')]
    operations=[migrations.AddField(model_name='applicationevent',name='consultation_event',
        field=models.OneToOneField(null=True,on_delete=django.db.models.deletion.PROTECT,to='consultations.consultationevent')),
        migrations.RunSQL(SQL)]
