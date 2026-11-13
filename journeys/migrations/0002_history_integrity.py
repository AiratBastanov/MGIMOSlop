from django.db import migrations

SQL="""
CREATE FUNCTION protect_journey() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR NEW.owner_id<>OLD.owner_id OR NEW.template_key<>OLD.template_key
 OR NEW.template_revision<>OLD.template_revision OR NEW.created_at<>OLD.created_at
 OR NEW.version<>OLD.version+1 OR NEW.updated_at<OLD.updated_at THEN
 RAISE EXCEPTION 'Journey ownership, template and version are protected' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER protect_journey BEFORE UPDATE OR DELETE ON journeys_journey FOR EACH ROW EXECUTE FUNCTION protect_journey();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON journeys_journeyrevision FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON journeys_journeysubmission FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON journeys_journeyoperation FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON journeys_journeybooking FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE FUNCTION require_journey_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM journeys_journeyrevision r WHERE r.journey_id=NEW.id AND r.version=NEW.version AND r.state=NEW.state) THEN
 RAISE EXCEPTION 'Saved preparation requires immutable revision' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER required_revision AFTER INSERT OR UPDATE ON journeys_journey
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_journey_revision();
CREATE FUNCTION validate_journey_link() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_TABLE_NAME='journeys_journeysubmission' THEN
   IF NOT EXISTS(SELECT 1 FROM journeys_journey j JOIN journeys_journeyrevision r ON r.journey_id=j.id
      JOIN student_requests_request q ON q.id=NEW.request_id AND q.owner_id=j.owner_id
      WHERE j.id=NEW.journey_id AND r.id=NEW.revision_id
      AND ((j.template_key='correction' AND q.request_type='academic_correction') OR
        (j.template_key='exchange' AND q.request_type='exchange_plan_review' AND q.payload_schema=3
        AND (q.payload_data->>'journey')::bigint=j.id AND (q.payload_data->>'journey_revision')::int=r.version))) THEN
      RAISE EXCEPTION 'Journey request link must retain owner and exact revision' USING ERRCODE='23514'; END IF;
 ELSE
   IF NOT EXISTS(SELECT 1 FROM journeys_journey j JOIN consultations_booking b ON b.student_id=j.owner_id
      WHERE j.id=NEW.journey_id AND b.id=NEW.booking_id) THEN
      RAISE EXCEPTION 'Journey booking belongs to another owner' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER validate_link BEFORE INSERT ON journeys_journeysubmission FOR EACH ROW EXECUTE FUNCTION validate_journey_link();
CREATE TRIGGER validate_link BEFORE INSERT ON journeys_journeybooking FOR EACH ROW EXECUTE FUNCTION validate_journey_link();
ALTER TABLE student_requests_request ADD CONSTRAINT request_exchange_snapshot CHECK (
 payload_schema<>3 OR (jsonb_typeof(payload_data)='object'
 AND payload_data ?& ARRAY['schema','journey','journey_revision','template_revision','program','source','checks','documents','questions','shared_note']
 AND payload_data->>'schema'='1' AND payload_data->>'template_revision'='1'
 AND jsonb_typeof(payload_data->'checks')='array' AND jsonb_typeof(payload_data->'documents')='array'
 AND jsonb_array_length(payload_data->'documents')=4));
"""

class Migration(migrations.Migration):
    dependencies=[('journeys','0001_initial')]
    operations=[migrations.RunSQL(SQL)]
