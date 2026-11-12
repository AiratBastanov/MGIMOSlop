from django.db import migrations


TABLES = [
    'data_import_academiccoverage',
    'studies_enrollmentchange',
    'studies_finalresultchange',
    'studies_requirementchange',
    'studies_assessmentchange',
]


class Migration(migrations.Migration):
    dependencies = [
        ('data_import', '0004_academiccoverage'),
        ('studies', '0004_assessmentevent_assessmentchange_enrollment_and_more'),
        ('notifications', '0004_remove_notification_notification_typed_target_and_more'),
    ]
    operations = [migrations.RunSQL(
        '\n'.join(f'CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON {table} '
                  'FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();' for table in TABLES),
        '\n'.join(f'DROP TRIGGER immutable_history ON {table};' for table in TABLES),
    )]
