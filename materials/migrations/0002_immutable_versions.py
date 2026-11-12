from django.db import migrations


SQL="""
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON materials_submissionpolicy
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON materials_materialrevision
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON materials_materialreview
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

ALTER TABLE materials_material ADD CONSTRAINT material_text_bounds
  CHECK (char_length(draft_title) BETWEEN 1 AND 160 AND char_length(draft_content) <= 20000);
ALTER TABLE materials_materialreview ADD CONSTRAINT material_feedback_bounds
  CHECK (char_length(feedback) BETWEEN 1 AND 2000 AND feedback ~ '[^[:space:]]');
ALTER TABLE materials_submissionpolicy ADD CONSTRAINT material_policy_shape CHECK (
  jsonb_typeof(modes) = 'array' AND jsonb_array_length(modes) > 0
  AND modes <@ '["text","link","email"]'::jsonb
  AND jsonb_typeof(cloud_hosts) = 'array'
  AND cloud_hosts <@ '["drive.google.com","docs.google.com","disk.yandex.ru","1drv.ms","onedrive.live.com"]'::jsonb
  AND (NOT modes ? 'link' OR jsonb_array_length(cloud_hosts) > 0));

CREATE FUNCTION protect_material() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Material history cannot be deleted' USING ERRCODE = '23514';
  END IF;
  IF NEW.owner_id <> OLD.owner_id OR NEW.assignment_id <> OLD.assignment_id
      OR NEW.created_at <> OLD.created_at OR NEW.id <> OLD.id THEN
    RAISE EXCEPTION 'Material ownership is immutable' USING ERRCODE = '23514';
  END IF;
  IF NEW.draft_version <> OLD.draft_version + 1 OR NEW.submission_count < OLD.submission_count
      OR NEW.submission_count > OLD.submission_count + 1 THEN
    RAISE EXCEPTION 'Material version is invalid' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER protect_material BEFORE UPDATE OR DELETE ON materials_material
  FOR EACH ROW EXECUTE FUNCTION protect_material();

CREATE FUNCTION validate_material_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM materials_material m JOIN materials_submissionpolicy p
    ON p.assignment_id=m.assignment_id WHERE m.id=NEW.material_id AND p.id=NEW.policy_id
      AND m.has_draft AND NEW.number=m.submission_count+1 AND NEW.title=m.draft_title
      AND NEW.mode=m.draft_mode AND NEW.content=m.draft_content AND p.modes ? NEW.mode) THEN
    RAISE EXCEPTION 'Submitted revision must match its saved draft and policy' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER validate_material_revision BEFORE INSERT ON materials_materialrevision
  FOR EACH ROW EXECUTE FUNCTION validate_material_revision();

CREATE FUNCTION require_material_revisions() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target_id bigint;
BEGIN
  IF TG_TABLE_NAME = 'materials_material' THEN target_id=NEW.id;
  ELSE target_id=NEW.material_id; END IF;
  IF EXISTS (SELECT 1 FROM materials_material m WHERE m.id=target_id
      AND m.submission_count <> (SELECT count(*) FROM materials_materialrevision r WHERE r.material_id=m.id)) THEN
    RAISE EXCEPTION 'Material requires all submitted versions' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER require_material_versions AFTER INSERT OR UPDATE ON materials_material
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_material_revisions();
CREATE CONSTRAINT TRIGGER require_material_counter AFTER INSERT ON materials_materialrevision
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_material_revisions();

CREATE FUNCTION protect_teaching_assignment() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' OR NEW.teacher_id<>OLD.teacher_id OR NEW.course_id<>OLD.course_id
      OR NEW.group_id<>OLD.group_id OR NEW.period_id<>OLD.period_id
      OR NEW.available_from<>OLD.available_from OR NEW.available_until<>OLD.available_until THEN
    RAISE EXCEPTION 'Teaching binding is immutable; deactivate and provision a new assignment' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER protect_teaching_assignment BEFORE UPDATE OR DELETE ON materials_teachingassignment
  FOR EACH ROW EXECUTE FUNCTION protect_teaching_assignment();

CREATE FUNCTION validate_material_review() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM materials_materialrevision r
    JOIN materials_material m ON m.id=r.material_id
    JOIN materials_teachingassignment a ON a.id=m.assignment_id
    WHERE r.id=NEW.revision_id AND a.teacher_id=NEW.teacher_id) THEN
    RAISE EXCEPTION 'Review belongs to the assigned teacher and exact revision' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER validate_material_review BEFORE INSERT ON materials_materialreview
  FOR EACH ROW EXECUTE FUNCTION validate_material_review();
"""

REVERSE="""
DROP TRIGGER validate_material_review ON materials_materialreview;
DROP FUNCTION validate_material_review();
DROP TRIGGER protect_teaching_assignment ON materials_teachingassignment;
DROP FUNCTION protect_teaching_assignment();
DROP TRIGGER require_material_counter ON materials_materialrevision;
DROP TRIGGER require_material_versions ON materials_material;
DROP FUNCTION require_material_revisions();
DROP TRIGGER validate_material_revision ON materials_materialrevision;
DROP FUNCTION validate_material_revision();
DROP TRIGGER protect_material ON materials_material;
DROP FUNCTION protect_material();
ALTER TABLE materials_material DROP CONSTRAINT material_text_bounds;
ALTER TABLE materials_materialreview DROP CONSTRAINT material_feedback_bounds;
ALTER TABLE materials_submissionpolicy DROP CONSTRAINT material_policy_shape;
DROP TRIGGER immutable_history ON materials_materialreview;
DROP TRIGGER immutable_history ON materials_materialrevision;
DROP TRIGGER immutable_history ON materials_submissionpolicy;
"""


class Migration(migrations.Migration):
    dependencies=[('materials','0001_initial'),('data_import','0003_immutable_history')]
    operations=[migrations.RunSQL(SQL,REVERSE)]
