from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [('profiles', '0001_initial'), ('data_import', '0003_immutable_history')]
    operations = [migrations.RunSQL('''
        CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON profiles_groupresourcerevision
        FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
    ''', 'DROP TRIGGER immutable_history ON profiles_groupresourcerevision;')]
