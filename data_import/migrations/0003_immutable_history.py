from django.db import migrations

TABLES = ['data_import_importrevision','data_import_coverage','studies_schedulechange','notifications_notification']

CREATE = """
CREATE FUNCTION reject_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Accepted import history is immutable' USING ERRCODE = '23514';
END;
$$;
""" + '\n'.join(f'CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();' for table in TABLES)
DROP = '\n'.join(f'DROP TRIGGER immutable_history ON {table};' for table in TABLES) + '\nDROP FUNCTION reject_history_mutation();'

class Migration(migrations.Migration):
    dependencies = [('data_import','0002_initial'),('notifications','0001_initial')]
    operations = [migrations.RunSQL(CREATE,DROP)]
