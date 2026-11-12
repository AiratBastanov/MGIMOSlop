from django.db import migrations, models


PROTECT = """
CREATE OR REPLACE FUNCTION protect_request_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Submitted request cannot be deleted' USING ERRCODE = '23514';
  END IF;
  IF NEW.id <> OLD.id OR NEW.owner_id <> OLD.owner_id
    OR NEW.servicing_faculty_id <> OLD.servicing_faculty_id
    OR NEW.request_type <> OLD.request_type OR NEW.comment <> OLD.comment
    OR NEW.submission_key <> OLD.submission_key OR NEW.payload_fingerprint <> OLD.payload_fingerprint
    OR NEW.created_at <> OLD.created_at OR NEW.provenance <> OLD.provenance
    OR NEW.payload_schema IS DISTINCT FROM OLD.payload_schema
    OR NEW.payload_data IS DISTINCT FROM OLD.payload_data
    OR NEW.applicant_snapshot IS DISTINCT FROM OLD.applicant_snapshot THEN
    RAISE EXCEPTION 'Submitted payload and routing are immutable' USING ERRCODE = '23514';
  END IF;
  IF NEW.version <> OLD.version + 1 OR NEW.updated_at < OLD.updated_at OR NOT (
    (OLD.status = 'submitted' AND NEW.status IN ('in_progress', 'rejected')) OR
    (OLD.status = 'in_progress' AND NEW.status IN ('ready', 'rejected'))
  ) THEN
    RAISE EXCEPTION 'Request transition or version is invalid' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
"""
LEGACY_PROTECT = PROTECT.replace(
    '\n    OR NEW.payload_schema IS DISTINCT FROM OLD.payload_schema'
    '\n    OR NEW.payload_data IS DISTINCT FROM OLD.payload_data'
    '\n    OR NEW.applicant_snapshot IS DISTINCT FROM OLD.applicant_snapshot', '')


class Migration(migrations.Migration):
    dependencies = [('student_requests', '0002_request_integrity')]
    operations = [
        migrations.AddField(model_name='request', name='payload_schema',
                            field=models.PositiveSmallIntegerField(default=1)),
        migrations.AddField(model_name='request', name='payload_data', field=models.JSONField(default=dict)),
        migrations.AddField(model_name='request', name='applicant_snapshot', field=models.JSONField(default=dict)),
        migrations.RemoveConstraint(model_name='request', name='request_supported_type'),
        migrations.AddConstraint(model_name='request', constraint=models.CheckConstraint(
            condition=models.Q(request_type__in=['study_certificate', 'academic_correction']), name='request_supported_type')),
        migrations.AddConstraint(model_name='request', constraint=models.CheckConstraint(
            condition=(models.Q(payload_schema=1, request_type='study_certificate', payload_data={}, applicant_snapshot={}) |
                       models.Q(payload_schema=2)), name='request_payload_schema')),
        migrations.RunSQL(PROTECT, LEGACY_PROTECT),
    ]
