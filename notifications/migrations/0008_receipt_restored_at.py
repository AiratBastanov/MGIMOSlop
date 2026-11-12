from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies=[('notifications','0007_initial_receipts')]
    operations=[migrations.AddField(model_name='notificationreceipt',name='restored_at',field=models.DateTimeField(null=True))]
