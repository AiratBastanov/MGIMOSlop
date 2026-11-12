from django.db import migrations


SQL = """
ALTER TABLE student_requests_request ADD CONSTRAINT request_comment_length CHECK (char_length(comment) <= 500);
ALTER TABLE student_requests_requestevent ADD CONSTRAINT request_explanation_length CHECK (char_length(explanation) <= 500);
ALTER TABLE student_requests_requestevent ADD CONSTRAINT request_explanation_nonblank
  CHECK (action NOT IN ('reject', 'complete') OR explanation ~ '[^[:space:]]');

CREATE FUNCTION protect_request_state() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'Submitted request cannot be deleted' USING ERRCODE = '23514';
  END IF;
  IF NEW.id <> OLD.id OR NEW.owner_id <> OLD.owner_id
    OR NEW.servicing_faculty_id <> OLD.servicing_faculty_id
    OR NEW.request_type <> OLD.request_type OR NEW.comment <> OLD.comment
    OR NEW.submission_key <> OLD.submission_key OR NEW.payload_fingerprint <> OLD.payload_fingerprint
    OR NEW.created_at <> OLD.created_at OR NEW.provenance <> OLD.provenance THEN
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
CREATE TRIGGER protect_request BEFORE UPDATE OR DELETE ON student_requests_request
  FOR EACH ROW EXECUTE FUNCTION protect_request_state();

CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON student_requests_requestevent
  FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

CREATE FUNCTION validate_request_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM student_requests_request q
      WHERE q.id = NEW.request_id AND q.version = NEW.version AND q.status = NEW.to_status
        AND q.updated_at = NEW.recorded_at) THEN
    RAISE EXCEPTION 'Request event must describe current committed candidate' USING ERRCODE = '23514';
  END IF;
  IF NEW.version > 1 AND NOT EXISTS (SELECT 1 FROM student_requests_requestevent e
      WHERE e.request_id = NEW.request_id AND e.version = NEW.version - 1
        AND e.to_status = NEW.from_status AND e.recorded_at <= NEW.recorded_at) THEN
    RAISE EXCEPTION 'Request event chain is invalid' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER validate_event BEFORE INSERT ON student_requests_requestevent
  FOR EACH ROW EXECUTE FUNCTION validate_request_event();

CREATE FUNCTION require_request_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM student_requests_requestevent e
      WHERE e.request_id = NEW.id AND e.version = NEW.version AND e.to_status = NEW.status
        AND e.recorded_at = NEW.updated_at) THEN
    RAISE EXCEPTION 'Request version requires its immutable event' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER required_event AFTER INSERT OR UPDATE ON student_requests_request
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_request_event();

CREATE FUNCTION require_request_notification() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM notifications_notification n
      WHERE n.request_event_id = NEW.id AND n.published_at = NEW.recorded_at) THEN
    RAISE EXCEPTION 'Request event requires its personal notification' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER required_notification AFTER INSERT ON student_requests_requestevent
  DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_request_notification();
"""

REVERSE = """
DROP TRIGGER required_notification ON student_requests_requestevent;
DROP FUNCTION require_request_notification();
DROP TRIGGER required_event ON student_requests_request;
DROP FUNCTION require_request_event();
DROP TRIGGER validate_event ON student_requests_requestevent;
DROP FUNCTION validate_request_event();
DROP TRIGGER immutable_history ON student_requests_requestevent;
DROP TRIGGER protect_request ON student_requests_request;
DROP FUNCTION protect_request_state();
ALTER TABLE student_requests_requestevent DROP CONSTRAINT request_explanation_nonblank;
ALTER TABLE student_requests_requestevent DROP CONSTRAINT request_explanation_length;
ALTER TABLE student_requests_request DROP CONSTRAINT request_comment_length;
"""


class Migration(migrations.Migration):
    dependencies = [
        ('student_requests', '0001_initial'),
        ('notifications', '0005_request_event_target'),
    ]
    operations = [migrations.RunSQL(SQL, REVERSE)]
