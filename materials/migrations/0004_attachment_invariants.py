from django.db import migrations

SQL=r"""
ALTER TABLE materials_materialattachment ADD CONSTRAINT attachment_bytes_shape CHECK (
  size BETWEEN 1 AND 10485760 AND size=octet_length(data)
  AND sha256=encode(sha256(data),'hex') AND char_length(name) BETWEEN 1 AND 160
  AND kind IN ('pdf','docx','txt','jpeg','png','webp'));
ALTER TABLE materials_submissionpolicy ADD CONSTRAINT attachment_policy_shape CHECK (
  attachment_count BETWEEN 1 AND 5 AND attachment_file_limit BETWEEN 1 AND 10485760
  AND attachment_total_limit BETWEEN 1 AND 26214400
  AND jsonb_typeof(attachment_types)='array'
  AND attachment_types <@ '["pdf","docx","txt","jpeg","png","webp"]'::jsonb
  AND (NOT attachments_allowed OR jsonb_array_length(attachment_types)>0));
ALTER TABLE materials_materialrevision ADD CONSTRAINT attachment_manifest_shape CHECK (
  jsonb_typeof(attachment_manifest)='array' AND jsonb_array_length(attachment_manifest)<=5);
CREATE TRIGGER immutable_bytes BEFORE UPDATE ON materials_materialattachment
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_receipt BEFORE UPDATE OR DELETE ON materials_materialoperation
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_revision_files BEFORE UPDATE OR DELETE ON materials_revisionattachment
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

CREATE FUNCTION validate_attachment_link() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target bigint;
BEGIN
  IF TG_TABLE_NAME='materials_revisionattachment' THEN
    SELECT r.material_id INTO target FROM materials_materialrevision r JOIN materials_material m ON m.id=r.material_id
      WHERE r.id=NEW.revision_id AND m.has_draft AND r.number=m.submission_count+1
        AND r.attachment_manifest->>NEW.position=NEW.attachment_id::text;
  ELSE
    SELECT id INTO target FROM materials_material WHERE id=NEW.material_id AND has_draft;
  END IF;
  IF target IS NULL OR NEW.position NOT BETWEEN 0 AND 4 OR NOT EXISTS (
    SELECT 1 FROM materials_materialattachment WHERE id=NEW.attachment_id AND material_id=target) THEN
    RAISE EXCEPTION 'Attachment binding is not authorized or revision is sealed' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER validate_draft_attachment BEFORE INSERT OR UPDATE ON materials_draftattachment
  FOR EACH ROW EXECUTE FUNCTION validate_attachment_link();
CREATE TRIGGER validate_revision_attachment BEFORE INSERT ON materials_revisionattachment
  FOR EACH ROW EXECUTE FUNCTION validate_attachment_link();

CREATE FUNCTION validate_attachment_manifest() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.attachment_manifest <> COALESCE((SELECT jsonb_agg(attachment_id::text ORDER BY position)
      FROM materials_draftattachment WHERE material_id=NEW.material_id),'[]'::jsonb) THEN
    RAISE EXCEPTION 'Revision attachment manifest must equal its exact saved draft set' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER validate_attachment_manifest BEFORE INSERT ON materials_materialrevision
  FOR EACH ROW EXECUTE FUNCTION validate_attachment_manifest();

CREATE FUNCTION require_revision_attachments() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE target bigint;
BEGIN
  IF TG_TABLE_NAME='materials_materialrevision' THEN target=NEW.id; ELSE target=NEW.revision_id; END IF;
  IF EXISTS(SELECT 1 FROM materials_materialrevision r WHERE r.id=target AND r.attachment_manifest <>
      COALESCE((SELECT jsonb_agg(attachment_id::text ORDER BY position) FROM materials_revisionattachment WHERE revision_id=r.id),'[]'::jsonb)) THEN
    RAISE EXCEPTION 'Revision requires its exact attachment set' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER revision_requires_attachments AFTER INSERT ON materials_materialrevision
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_revision_attachments();
CREATE CONSTRAINT TRIGGER attachment_requires_manifest AFTER INSERT ON materials_revisionattachment
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_revision_attachments();

CREATE FUNCTION validate_material_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.fingerprint !~ '^[0-9a-f]{64}$' OR NEW.action NOT IN ('save','submit','correct') OR NOT EXISTS(
      SELECT 1 FROM materials_material WHERE id=NEW.material_id AND owner_id=NEW.owner_id) THEN
    RAISE EXCEPTION 'Invalid material receipt' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER validate_material_receipt BEFORE INSERT ON materials_materialoperation
  FOR EACH ROW EXECUTE FUNCTION validate_material_receipt();
"""

REVERSE="""
DROP TRIGGER validate_material_receipt ON materials_materialoperation;
DROP FUNCTION validate_material_receipt();
DROP TRIGGER attachment_requires_manifest ON materials_revisionattachment;
DROP TRIGGER revision_requires_attachments ON materials_materialrevision;
DROP FUNCTION require_revision_attachments();
DROP TRIGGER validate_attachment_manifest ON materials_materialrevision;
DROP FUNCTION validate_attachment_manifest();
DROP TRIGGER validate_revision_attachment ON materials_revisionattachment;
DROP TRIGGER validate_draft_attachment ON materials_draftattachment;
DROP FUNCTION validate_attachment_link();
DROP TRIGGER immutable_revision_files ON materials_revisionattachment;
DROP TRIGGER immutable_receipt ON materials_materialoperation;
DROP TRIGGER immutable_bytes ON materials_materialattachment;
ALTER TABLE materials_materialrevision DROP CONSTRAINT attachment_manifest_shape;
ALTER TABLE materials_submissionpolicy DROP CONSTRAINT attachment_policy_shape;
ALTER TABLE materials_materialattachment DROP CONSTRAINT attachment_bytes_shape;
"""

class Migration(migrations.Migration):
    dependencies=[('materials','0003_private_attachments')]
    operations=[migrations.RunSQL(SQL,REVERSE)]
