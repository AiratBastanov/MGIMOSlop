from django.db import migrations

SQL = """
INSERT INTO accounts_authorizationguard(id,revision) VALUES (1,1) ON CONFLICT DO NOTHING;
ALTER TABLE accounts_sessioncontext ADD CONSTRAINT exact_two_hours CHECK (expires_at = authenticated_at + interval '2 hours');
CREATE FUNCTION protect_session_context() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.id <> OLD.id OR NEW.user_id <> OLD.user_id OR NEW.session_digest <> OLD.session_digest
    OR NEW.authenticated_at <> OLD.authenticated_at OR NEW.expires_at <> OLD.expires_at
    OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
    RAISE EXCEPTION 'Session context identity and deadline are immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER protect_context BEFORE UPDATE ON accounts_sessioncontext FOR EACH ROW EXECUTE FUNCTION protect_session_context();
CREATE FUNCTION protect_identity_binding() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.user_id <> OLD.user_id OR NEW.profile_id IS DISTINCT FROM OLD.profile_id THEN
    RAISE EXCEPTION 'Identity binding is immutable' USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END; $$;
CREATE TRIGGER protect_identity BEFORE UPDATE ON accounts_identity FOR EACH ROW EXECUTE FUNCTION protect_identity_binding();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON studies_profilechange FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON accounts_accessaudit FOR EACH ROW EXECUTE FUNCTION reject_history_mutation();
"""

class Migration(migrations.Migration):
    dependencies = [('accounts','0002_initial'),('data_import','0003_immutable_history')]
    operations = [migrations.RunSQL(SQL)]
