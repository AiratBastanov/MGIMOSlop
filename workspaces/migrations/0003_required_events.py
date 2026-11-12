from django.db import migrations

SQL=r"""
CREATE FUNCTION require_workspace_event() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE present boolean;
BEGIN
 EXECUTE format('SELECT EXISTS(SELECT 1 FROM workspaces_applicationevent WHERE %I=$1)',TG_ARGV[0]) INTO present USING NEW.id;
 IF NOT present THEN RAISE EXCEPTION 'Published operation requires application event' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER required_application_event AFTER INSERT ON workspaces_teachingrevision
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_workspace_event('teaching_revision_id');
CREATE CONSTRAINT TRIGGER required_application_event AFTER INSERT ON workspaces_staffnoticerevision
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_workspace_event('notice_revision_id');
CREATE CONSTRAINT TRIGGER required_application_event AFTER INSERT ON workspaces_clarificationmessage
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_workspace_event('clarification_message_id');

CREATE FUNCTION validate_notice_revision() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM workspaces_staffnotice n JOIN accounts_staffscope s ON s.faculty_id=n.faculty_id WHERE n.id=NEW.notice_id AND s.identity_id=NEW.author_id)
 OR NEW.number<>COALESCE((SELECT max(number)+1 FROM workspaces_staffnoticerevision WHERE notice_id=NEW.notice_id),1)
 OR NEW.state NOT IN ('published','withdrawn') OR char_length(NEW.payload->>'title') NOT BETWEEN 1 AND 180 THEN
 RAISE EXCEPTION 'Invalid notice revision' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER validate_notice_revision BEFORE INSERT ON workspaces_staffnoticerevision
 FOR EACH ROW EXECUTE FUNCTION validate_notice_revision();

CREATE FUNCTION validate_clarification_message() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM workspaces_clarification c WHERE c.id=NEW.clarification_id AND c.version=NEW.number
   AND ((c.state='open' AND NEW.action='ask' AND NEW.number=1) OR (c.state='answered' AND NEW.action='reply')
     OR (c.state='resolved' AND NEW.action IN ('resolve','close'))))
 OR char_length(NEW.body) NOT BETWEEN 1 AND 2000
 OR NEW.number<>COALESCE((SELECT max(number)+1 FROM workspaces_clarificationmessage WHERE clarification_id=NEW.clarification_id),1) THEN
 RAISE EXCEPTION 'Invalid clarification message' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER validate_clarification_message BEFORE INSERT ON workspaces_clarificationmessage
 FOR EACH ROW EXECUTE FUNCTION validate_clarification_message();

CREATE FUNCTION require_clarification_message() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM workspaces_clarificationmessage WHERE clarification_id=NEW.id AND number=NEW.version) THEN
 RAISE EXCEPTION 'Clarification transition requires message' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE CONSTRAINT TRIGGER required_message AFTER INSERT OR UPDATE ON workspaces_clarification
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION require_clarification_message();
"""

class Migration(migrations.Migration):
    dependencies=[('workspaces','0002_invariants')]
    operations=[migrations.RunSQL(SQL)]
