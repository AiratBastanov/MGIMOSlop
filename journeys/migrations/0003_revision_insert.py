from django.db import migrations

SQL="""
CREATE FUNCTION validate_journey_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM journeys_journey j WHERE j.id=NEW.journey_id AND j.version=NEW.version AND j.state=NEW.state) THEN
 RAISE EXCEPTION 'Inserted revision must match current preparation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER validate_revision BEFORE INSERT ON journeys_journeyrevision FOR EACH ROW EXECUTE FUNCTION validate_journey_revision();
"""

class Migration(migrations.Migration):
    dependencies=[('journeys','0002_history_integrity')]
    operations=[migrations.RunSQL(SQL)]
