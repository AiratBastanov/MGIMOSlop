from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('accounts', '0003_guard_and_immutability'), ('studies', '0004_assessmentevent_assessmentchange_enrollment_and_more')]
    operations = [
        migrations.RemoveConstraint(model_name='identity', name='identity_role_binding'),
        migrations.AddField(model_name='identity', name='instructor', field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, to='studies.instructor')),
        migrations.AlterField(model_name='identity', name='role', field=models.CharField(choices=[('student','Студент'),('staff','Сотрудник'),('teacher','Преподаватель')], max_length=10)),
        migrations.AddConstraint(model_name='identity', constraint=models.CheckConstraint(condition=(
            models.Q(role='student',profile__isnull=False,instructor__isnull=True) |
            models.Q(role='staff',profile__isnull=True,instructor__isnull=True) |
            models.Q(role='teacher',profile__isnull=True,instructor__isnull=False)), name='identity_role_binding')),
        migrations.RunSQL("""
CREATE OR REPLACE FUNCTION protect_identity_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.user_id <> OLD.user_id OR NEW.profile_id IS DISTINCT FROM OLD.profile_id
     OR NEW.instructor_id IS DISTINCT FROM OLD.instructor_id THEN
    RAISE EXCEPTION 'Identity binding is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
""", """
CREATE OR REPLACE FUNCTION protect_identity_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.user_id <> OLD.user_id OR NEW.profile_id IS DISTINCT FROM OLD.profile_id THEN
    RAISE EXCEPTION 'Identity binding is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
"""),
    ]
