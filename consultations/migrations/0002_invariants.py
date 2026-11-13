from django.db import migrations


SQL = r"""
CREATE FUNCTION protect_consultation_slot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Consultation history is retained' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND (NEW.id<>OLD.id OR NEW.host_id<>OLD.host_id OR NEW.kind<>OLD.kind
   OR NEW.assignment_id IS DISTINCT FROM OLD.assignment_id OR NEW.faculty_id IS DISTINCT FROM OLD.faculty_id
   OR NEW.starts_at<>OLD.starts_at OR NEW.ends_at<>OLD.ends_at OR NEW.topic<>OLD.topic
   OR NEW.created_at<>OLD.created_at OR NEW.demo_key IS DISTINCT FROM OLD.demo_key
   OR NEW.version<>OLD.version+1 OR OLD.state<>'open') THEN
   RAISE EXCEPTION 'Consultation identity, time and audience are immutable' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' AND (NEW.version<>1 OR NEW.state<>'open') THEN
   RAISE EXCEPTION 'Consultation must start open at version one' USING ERRCODE='23514'; END IF;
 IF char_length(NEW.topic) NOT BETWEEN 1 AND 160 OR char_length(NEW.venue)>240 OR char_length(NEW.meeting_url)>1000
   OR (NEW.venue='' AND NEW.meeting_url='') OR NEW.ends_at-NEW.starts_at NOT BETWEEN interval '5 minutes' AND interval '4 hours'
   OR NEW.version<1 OR NEW.updated_at<NEW.created_at THEN
   RAISE EXCEPTION 'Invalid consultation contents' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM accounts_identity i WHERE i.id=NEW.host_id AND (
   (NEW.kind='teacher' AND i.role='teacher' AND EXISTS(SELECT 1 FROM materials_teachingassignment a WHERE a.id=NEW.assignment_id AND a.teacher_id=i.id))
   OR (NEW.kind='staff' AND i.role='staff' AND EXISTS(SELECT 1 FROM accounts_staffscope s WHERE s.identity_id=i.id AND s.faculty_id=NEW.faculty_id)))) THEN
   RAISE EXCEPTION 'Invalid consultation host audience' USING ERRCODE='23514'; END IF;
 IF NEW.state<>'open' AND EXISTS(SELECT 1 FROM consultations_booking b WHERE b.slot_id=NEW.id AND b.state='booked') THEN
   RAISE EXCEPTION 'A booked consultation requires participant cancellation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER protect_consultation_slot BEFORE INSERT OR UPDATE OR DELETE ON consultations_consultationslot
 FOR EACH ROW EXECUTE FUNCTION protect_consultation_slot();

CREATE FUNCTION protect_consultation_booking() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE s consultations_consultationslot%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Booking history is retained' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' AND (NEW.id<>OLD.id OR NEW.slot_id<>OLD.slot_id OR NEW.student_id<>OLD.student_id
   OR NEW.starts_at<>OLD.starts_at OR NEW.ends_at<>OLD.ends_at OR NEW.question<>OLD.question
   OR NEW.request_id IS DISTINCT FROM OLD.request_id OR NEW.academic_ack<>OLD.academic_ack
   OR NEW.replaces_id IS DISTINCT FROM OLD.replaces_id OR NEW.created_at<>OLD.created_at
   OR NEW.version<>OLD.version+1 OR OLD.state<>'booked' OR NEW.state NOT IN ('cancelled','completed')) THEN
   RAISE EXCEPTION 'Invalid booking transition or identity change' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' AND (NEW.version<>1 OR NEW.state<>'booked') THEN
   RAISE EXCEPTION 'Booking must start confirmed at version one' USING ERRCODE='23514'; END IF;
 SELECT * INTO s FROM consultations_consultationslot WHERE id=NEW.slot_id;
 IF s.id IS NULL OR NEW.starts_at<>s.starts_at OR NEW.ends_at<>s.ends_at
   OR (TG_OP='INSERT' AND s.state<>'open') OR char_length(NEW.question)>1000 OR NEW.version<1
   OR NEW.updated_at<NEW.created_at THEN
   RAISE EXCEPTION 'Booking must pin the exact slot' USING ERRCODE='23514'; END IF;
 IF NEW.request_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM student_requests_request r
    WHERE r.id=NEW.request_id AND s.kind='staff' AND r.owner_id=NEW.student_id AND r.servicing_faculty_id=s.faculty_id) THEN
   RAISE EXCEPTION 'Request context is restricted to its faculty service' USING ERRCODE='23514'; END IF;
 IF TG_OP='INSERT' AND NEW.replaces_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM consultations_booking b
   WHERE b.id=NEW.replaces_id AND b.student_id=NEW.student_id AND b.state='cancelled' AND b.slot_id<>NEW.slot_id) THEN
   RAISE EXCEPTION 'Transfer must keep a cancelled predecessor owned by the same student' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER protect_consultation_booking BEFORE INSERT OR UPDATE OR DELETE ON consultations_booking
 FOR EACH ROW EXECUTE FUNCTION protect_consultation_booking();

CREATE FUNCTION validate_consultation_event() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM consultations_consultationslot s WHERE s.id=NEW.slot_id AND s.version=NEW.slot_version)
   OR jsonb_typeof(NEW.payload)<>'object' OR NOT (NEW.payload ?& ARRAY['topic','starts_at','ends_at','time_label','slot_state','booking_state'])
   OR (NEW.booking_id IS NULL AND NEW.booking_version IS NOT NULL)
   OR (NEW.booking_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM consultations_booking b WHERE b.id=NEW.booking_id
      AND b.slot_id=NEW.slot_id AND b.version=NEW.booking_version)) THEN
   RAISE EXCEPTION 'Event must describe the exact consultation version' USING ERRCODE='23514'; END IF;
 IF NEW.action IN ('booked','cancelled','transferred','completed') AND NEW.booking_id IS NULL THEN
   RAISE EXCEPTION 'Booking event requires a booking' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM consultations_consultationslot s JOIN accounts_identity host ON host.id=s.host_id
    WHERE s.id=NEW.slot_id AND (host.user_id=NEW.actor_id OR
      (NEW.action IN ('booked','cancelled','transferred') AND EXISTS(SELECT 1 FROM consultations_booking b
        JOIN accounts_identity i ON i.profile_id=b.student_id WHERE b.id=NEW.booking_id AND i.user_id=NEW.actor_id)))) THEN
   RAISE EXCEPTION 'Consultation event actor is not a participant' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER validate_consultation_event BEFORE INSERT ON consultations_consultationevent
 FOR EACH ROW EXECUTE FUNCTION validate_consultation_event();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON consultations_consultationevent
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON consultations_consultationoperation
 FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();

CREATE FUNCTION require_consultation_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE present boolean;
BEGIN
 IF TG_TABLE_NAME='consultations_consultationslot' THEN
   SELECT EXISTS(SELECT 1 FROM consultations_consultationevent WHERE slot_id=NEW.id AND slot_version=NEW.version)
      INTO present;
 ELSE
   SELECT EXISTS(SELECT 1 FROM consultations_consultationevent WHERE booking_id=NEW.id AND booking_version=NEW.version)
      INTO present;
 END IF;
 IF NOT present THEN RAISE EXCEPTION 'Consultation operation requires immutable event' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER required_consultation_event AFTER INSERT OR UPDATE ON consultations_consultationslot
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_consultation_event();
CREATE CONSTRAINT TRIGGER required_consultation_event AFTER INSERT OR UPDATE ON consultations_booking
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_consultation_event();
CREATE CONSTRAINT TRIGGER required_application_event AFTER INSERT ON consultations_consultationevent
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_workspace_event('consultation_event_id');
"""


class Migration(migrations.Migration):
    dependencies = [('consultations', '0001_initial'), ('workspaces', '0004_consultation_events')]
    operations = [migrations.RunSQL(SQL)]
