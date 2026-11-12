from django.db import migrations

SQL=r"""
ALTER TABLE workspaces_applicationevent ADD CONSTRAINT application_event_exact_target CHECK (
  num_nonnulls(teaching_revision_id,submission_id,review_id,clarification_message_id,notice_revision_id,resource_revision_id)=1
  AND ((kind='teaching' AND teaching_revision_id IS NOT NULL) OR (kind='submission' AND submission_id IS NOT NULL)
    OR (kind='review' AND review_id IS NOT NULL) OR (kind='clarification' AND clarification_message_id IS NOT NULL)
    OR (kind='notice' AND notice_revision_id IS NOT NULL) OR (kind='resource' AND resource_revision_id IS NOT NULL)));
ALTER TABLE workspaces_publicationfile ADD CONSTRAINT publication_bytes_shape CHECK (
  size BETWEEN 1 AND 10485760 AND size=octet_length(data) AND sha256=encode(sha256(data),'hex')
  AND char_length(name) BETWEEN 1 AND 160 AND kind IN ('pdf','docx','txt','jpeg','png','webp'));
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON workspaces_teachingrevision
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON workspaces_staffnoticerevision
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON workspaces_clarificationmessage
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON workspaces_taskmember
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON workspaces_publicationfile
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON workspaces_workspaceoperation
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON workspaces_applicationevent
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON workspaces_coursetask
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

CREATE FUNCTION protect_workspace_publication() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Publication history is retained' USING ERRCODE='23514'; END IF;
  IF NEW.id<>OLD.id OR NEW.created_at<>OLD.created_at OR NEW.version<>OLD.version+1 THEN
    RAISE EXCEPTION 'Publication version is invalid' USING ERRCODE='23514'; END IF;
  IF TG_TABLE_NAME='workspaces_teachingpublication' THEN
    IF NEW.assignment_id<>OLD.assignment_id OR NEW.kind<>OLD.kind THEN
      RAISE EXCEPTION 'Teaching audience is immutable' USING ERRCODE='23514'; END IF;
    IF NEW.published_revision_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM workspaces_teachingrevision
       WHERE id=NEW.published_revision_id AND publication_id=NEW.id AND state=NEW.state) THEN
      RAISE EXCEPTION 'Invalid published teaching revision' USING ERRCODE='23514'; END IF;
  ELSE
    IF NEW.faculty_id<>OLD.faculty_id OR NEW.group_id IS DISTINCT FROM OLD.group_id OR NEW.author_id<>OLD.author_id THEN
      RAISE EXCEPTION 'Notice audience is immutable' USING ERRCODE='23514'; END IF;
    IF NEW.published_revision_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM workspaces_staffnoticerevision
       WHERE id=NEW.published_revision_id AND notice_id=NEW.id AND state=NEW.state) THEN
      RAISE EXCEPTION 'Invalid published notice revision' USING ERRCODE='23514'; END IF;
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER protect_publication BEFORE UPDATE OR DELETE ON workspaces_teachingpublication
 FOR EACH ROW EXECUTE FUNCTION protect_workspace_publication();
CREATE TRIGGER protect_publication BEFORE UPDATE OR DELETE ON workspaces_staffnotice
 FOR EACH ROW EXECUTE FUNCTION protect_workspace_publication();

CREATE FUNCTION validate_teaching_revision() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE file_id text;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM workspaces_teachingpublication p JOIN materials_teachingassignment a ON a.id=p.assignment_id
   WHERE p.id=NEW.publication_id AND a.teacher_id=NEW.author_id) OR NEW.number<>COALESCE((SELECT max(number)+1 FROM workspaces_teachingrevision WHERE publication_id=NEW.publication_id),1)
   OR NEW.state NOT IN ('published','withdrawn','archived') OR jsonb_typeof(NEW.payload)<>'object'
   OR char_length(NEW.payload->>'title') NOT BETWEEN 1 AND 160 OR jsonb_typeof(NEW.files)<>'array' OR jsonb_array_length(NEW.files)>5 THEN
   RAISE EXCEPTION 'Invalid teaching revision' USING ERRCODE='23514'; END IF;
 FOR file_id IN SELECT jsonb_array_elements_text(NEW.files) LOOP
   IF NOT EXISTS(SELECT 1 FROM workspaces_publicationfile WHERE id=file_id::uuid AND publication_id=NEW.publication_id) THEN
     RAISE EXCEPTION 'File belongs to another publication' USING ERRCODE='23514'; END IF;
 END LOOP;
 RETURN NEW;
END; $$;
CREATE TRIGGER validate_teaching_revision BEFORE INSERT ON workspaces_teachingrevision
 FOR EACH ROW EXECUTE FUNCTION validate_teaching_revision();

CREATE FUNCTION validate_task_response() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_TABLE_NAME='materials_material' THEN
   IF TG_OP='UPDATE' AND NEW.task_id IS DISTINCT FROM OLD.task_id THEN RAISE EXCEPTION 'Task binding is immutable' USING ERRCODE='23514'; END IF;
   IF NEW.task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM workspaces_coursetask t JOIN workspaces_teachingpublication p ON p.id=t.publication_id
       JOIN workspaces_taskmember m ON m.task_id=t.id WHERE t.id=NEW.task_id AND p.kind='task' AND p.assignment_id=NEW.assignment_id AND m.student_id=NEW.owner_id) THEN
     RAISE EXCEPTION 'Response requires assigned cohort' USING ERRCODE='23514'; END IF;
 ELSE
   IF NOT EXISTS(SELECT 1 FROM materials_material m JOIN materials_submissionpolicy s ON s.id=NEW.policy_id
      WHERE m.id=NEW.material_id AND ((m.task_id IS NULL AND NEW.task_revision_id IS NULL AND s.task_revision_id IS NULL AND NEW.task_conditions='{}'::jsonb)
        OR (m.task_id IS NOT NULL AND NEW.task_revision_id=s.task_revision_id AND EXISTS(SELECT 1 FROM workspaces_coursetask t JOIN workspaces_teachingrevision r ON r.publication_id=t.publication_id
          WHERE t.id=m.task_id AND r.id=NEW.task_revision_id) AND NEW.task_conditions ?& ARRAY['late','due_at','cutoff_at','assigned_at','accepted_at']))) THEN
     RAISE EXCEPTION 'Task response must pin its task and policy' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER task_binding BEFORE INSERT OR UPDATE ON materials_material FOR EACH ROW EXECUTE FUNCTION validate_task_response();
CREATE TRIGGER task_binding BEFORE INSERT ON materials_materialrevision FOR EACH ROW EXECUTE FUNCTION validate_task_response();

CREATE FUNCTION require_application_notification() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM notifications_notification WHERE application_event_id=NEW.id) THEN
   RAISE EXCEPTION 'Application event requires notification' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER required_notification AFTER INSERT ON workspaces_applicationevent
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_application_notification();

CREATE FUNCTION protect_clarification() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR NEW.request_id<>OLD.request_id OR NEW.created_at<>OLD.created_at OR NEW.version<>OLD.version+1
    OR NOT ((OLD.state='open' AND NEW.state IN ('answered','resolved')) OR (OLD.state='answered' AND NEW.state='resolved')) THEN
   RAISE EXCEPTION 'Invalid clarification transition' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER protect_clarification BEFORE UPDATE OR DELETE ON workspaces_clarification
 FOR EACH ROW EXECUTE FUNCTION protect_clarification();
"""

class Migration(migrations.Migration):
    dependencies=[('workspaces','0001_initial'),('materials','0005_material_task_materialreview_result_and_more'),
        ('notifications','0010_notification_application_event_and_more')]
    operations=[migrations.RunSQL(SQL)]
