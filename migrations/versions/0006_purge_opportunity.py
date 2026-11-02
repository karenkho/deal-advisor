"""purge_opportunity: the one way to remove an opportunity's rows, audit entries included. The app role still cannot UPDATE or DELETE audit_log.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# children first, because the foreign keys run this way. A new table left out fails loudly: its foreign key refuses the
# delete of the opportunity, and the check at the end catches a table with an opportunity_id and no foreign key.
_TABLES = ("approvals", "change_requests", "artifact_versions", "br_reviews", "brs", "epics", "statements", "documents", "business_rules", "entities", "nfrs",
           "constraints_assumptions", "open_questions", "brd_section_items", "decisions", "edit_drafts", "runs", "id_counters")


def upgrade() -> None:
    tables = ", ".join(f"'{t}'" for t in _TABLES)
    op.execute(f"""
CREATE FUNCTION purge_opportunity(opportunity_id text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $fn$
DECLARE
    target text := opportunity_id;
    name text;
    n bigint;
    counts jsonb := '{{}}'::jsonb;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM opportunities o WHERE o.id = target) THEN
        RAISE EXCEPTION 'Unknown opportunity';
    END IF;
    FOREACH name IN ARRAY ARRAY[{tables}] LOOP
        EXECUTE format('DELETE FROM %I t WHERE t.opportunity_id = $1', name) USING target;
        GET DIAGNOSTICS n = ROW_COUNT;
        counts := counts || jsonb_build_object(name, n);
    END LOOP;
    DELETE FROM audit_log a WHERE a.opportunity_id = target OR (a.entity_type = 'opportunity' AND a.entity_id = target);
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('audit_log', n);
    DELETE FROM opportunities o WHERE o.id = target;
    GET DIAGNOSTICS n = ROW_COUNT;
    counts := counts || jsonb_build_object('opportunities', n);
    FOR name IN SELECT c.table_name FROM information_schema.columns c WHERE c.table_schema = 'public' AND c.column_name = 'opportunity_id' LOOP
        EXECUTE format('SELECT count(*) FROM %I t WHERE t.opportunity_id = $1', name) USING target INTO n;
        IF n > 0 THEN
            RAISE EXCEPTION 'purge_opportunity does not cover table %', name;
        END IF;
    END LOOP;
    RETURN counts;
END;
$fn$""")
    op.execute("ALTER FUNCTION purge_opportunity(text) OWNER TO dealadvisor_owner")
    op.execute("REVOKE ALL ON FUNCTION purge_opportunity(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION purge_opportunity(text) TO dealadvisor_app")


def downgrade() -> None:
    op.execute("DROP FUNCTION purge_opportunity(text)")
