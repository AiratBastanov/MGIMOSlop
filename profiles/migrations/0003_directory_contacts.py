from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies=[('profiles','0002_resource_history')]
    operations=[
        migrations.AddField(model_name='preferences',name='share_email',field=models.BooleanField(default=False)),
        migrations.AddField(model_name='preferences',name='directory_phone',field=models.CharField(max_length=24,blank=True)),
        migrations.AddField(model_name='preferences',name='share_phone',field=models.BooleanField(default=False)),
    ]
