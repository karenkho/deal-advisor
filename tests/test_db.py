"""Database tests against a throwaway PostgreSQL (tests/conftest.py): migrations, permissions, locking, IDs, import."""
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import func, inspect, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError, ProgrammingError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402
from sqlalchemy.orm.exc import StaleDataError  # noqa: E402

from app import legacy_import, models, store  # noqa: E402
from app.db import driver_url, make_engine, session_scope  # noqa: E402
from app.record import BrdItem, Brd, BusinessRequirement, Citation, Opportunity, OpportunityRecord, RecordFeature, RecordRequirement  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PLAN_TABLES = {"opportunities", "documents", "statements", "brs", "epics", "edit_drafts", "business_rules", "entities", "nfrs",
               "constraints_assumptions", "open_questions", "br_reviews", "decisions", "artifact_versions", "change_requests",
               "approvals", "audit_log", "runs"}


def _alembic(conn) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["connection"] = conn
    return cfg


@pytest.fixture(scope="module")
def migrated(pg):
    owner = make_engine(pg["owner"])
    with owner.begin() as conn:
        command.upgrade(_alembic(conn), "head")
    return pg


@pytest.fixture
def app_engine(migrated):
    """The runtime role, on empty tables."""
    owner = make_engine(migrated["owner"])
    with owner.begin() as conn:
        names = ", ".join(t for t in inspect(conn).get_table_names() if t != "alembic_version")
        conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
    return make_engine(migrated["app"])


def _opportunity(engine, opp_id="o1", code="O1") -> None:
    with session_scope(engine) as s:
        s.add(models.Opportunity(id=opp_id, code=code, name="Test project", client_name="Fictional client"))


# ---------- migrations ----------

def test_migrations_up_down_up_keep_the_audit_trail(migrated):
    owner = make_engine(migrated["owner"])
    with owner.connect() as conn:
        assert PLAN_TABLES <= set(inspect(conn).get_table_names())
    with owner.begin() as conn:
        conn.execute(text("INSERT INTO audit_log (actor_name, actor_role, action) VALUES ('t', 't', 'kept')"))
        command.downgrade(_alembic(conn), "base")
    with owner.connect() as conn:
        assert set(inspect(conn).get_table_names()) == {"audit_log", "alembic_version"}
        assert conn.execute(text("SELECT count(*) FROM audit_log WHERE action = 'kept'")).scalar_one() == 1
    with owner.begin() as conn:
        command.upgrade(_alembic(conn), "head")
        conn.execute(text("DELETE FROM audit_log WHERE action = 'kept'"))
    with owner.connect() as conn:
        assert PLAN_TABLES <= set(inspect(conn).get_table_names())


def test_models_match_the_migrations(migrated):
    with make_engine(migrated["owner"]).connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(ctx, models.Base.metadata) == []


# ---------- permissions ----------

@pytest.mark.parametrize("sql", ["UPDATE audit_log SET action = 'x'", "DELETE FROM audit_log", "TRUNCATE audit_log",
                                 "DROP TABLE audit_log", "ALTER TABLE audit_log DISABLE TRIGGER ALL",
                                 "UPDATE alembic_version SET version_num = 'x'", "DELETE FROM alembic_version",
                                 "CREATE TABLE sneaky (id int)"])
def test_app_role_is_refused(app_engine, sql):
    with app_engine.connect() as conn, pytest.raises(ProgrammingError, match="(?i)permission denied|must be owner"):
        conn.execute(text(sql))


def test_audit_survives_and_app_can_append_and_read(app_engine):
    with session_scope(app_engine) as s:
        store.audit(s, "Ana", "BDM", "opportunity.created", opportunity_id="o1", entity_type="opportunity", entity_id="o1", version="0.1",
                    details={"rates_source": "illustrative"})
    with session_scope(app_engine) as s:
        row = s.scalars(select(models.AuditLog)).one()
        assert (row.actor_name, row.actor_role, row.action, row.version) == ("Ana", "BDM", "opportunity.created", "0.1")
        assert row.occurred_at is not None


def test_a_failed_action_leaves_no_audit_entry(app_engine):
    with pytest.raises(RuntimeError), session_scope(app_engine) as s:
        store.audit(s, "Ana", "BDM", "x")
        s.flush()
        raise RuntimeError
    with session_scope(app_engine) as s:
        assert s.scalar(select(func.count()).select_from(models.AuditLog)) == 0


@pytest.mark.parametrize("details", [{"hourly_rate": 100}, {"rates": [1]}, {"nested": {"salary": 1}}, {"note": "write to a.person@example.com"}])
def test_audit_refuses_rates_and_personal_data(details):
    with pytest.raises(ValueError):
        store.audit(None, "Ana", "BDM", "x", details=details)


# ---------- optimistic locking ----------

def test_a_stale_save_is_refused_and_changes_nothing(app_engine):
    _opportunity(app_engine)
    with session_scope(app_engine) as s:
        seen = s.get(models.Opportunity, "o1").row_version
    with session_scope(app_engine) as s:
        store.update_checked(s, models.Opportunity, "o1", seen, bdm_name="First")
    with pytest.raises(store.StaleWrite, match="reload"), session_scope(app_engine) as s:
        store.update_checked(s, models.Opportunity, "o1", seen, bdm_name="Second")
    with session_scope(app_engine) as s:
        row = s.get(models.Opportunity, "o1")
        assert (row.bdm_name, row.row_version) == ("First", seen + 1)


def test_two_open_sessions_conflict_at_commit(app_engine):
    _opportunity(app_engine)
    with Session(app_engine) as a, Session(app_engine) as b:
        row_a, row_b = a.get(models.Opportunity, "o1"), b.get(models.Opportunity, "o1")
        row_a.bdm_name = "A"
        a.commit()
        row_b.bdm_name = "B"
        with pytest.raises(StaleDataError):
            b.commit()
    with session_scope(app_engine) as s:
        assert s.get(models.Opportunity, "o1").bdm_name == "A"


def test_parallel_sessions_lose_no_writes(app_engine):
    """Each worker reads, changes and saves the same row, retrying on a conflict: every increment must survive."""
    _opportunity(app_engine)
    conflicts = []

    def bump(_):
        while True:
            try:
                with session_scope(app_engine) as s:
                    row = s.get(models.Opportunity, "o1")
                    seen, count = row.row_version, row.commercial.get("count", 0)
                with session_scope(app_engine) as s:
                    store.update_checked(s, models.Opportunity, "o1", seen, commercial={"count": count + 1})
                return
            except store.StaleWrite:
                conflicts.append(1)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(bump, range(24)))
    with session_scope(app_engine) as s:
        row = s.get(models.Opportunity, "o1")
        assert row.commercial["count"] == 24
        assert row.row_version == 25


def test_the_same_artifact_version_cannot_be_written_twice(app_engine):
    _opportunity(app_engine)
    new = lambda: models.ArtifactVersion(opportunity_id="o1", artifact_type="BRD", version_major=0, version_minor=1,  # noqa: E731
                                         created_by="Ana", created_role="Solutions Specialist")
    with session_scope(app_engine) as s:
        s.add(new())
    with pytest.raises(IntegrityError), session_scope(app_engine) as s:
        s.add(new())


# ---------- stable IDs ----------

def test_ids_are_unique_in_parallel_and_never_reused(app_engine):
    _opportunity(app_engine)

    def next_id(_):
        with session_scope(app_engine) as s:
            return store.allocate_id(s, "o1", "BR")

    with ThreadPoolExecutor(8) as pool:
        ids = list(pool.map(next_id, range(24)))
    assert sorted(ids) == [f"BR-{i:03d}" for i in range(1, 25)]
    with session_scope(app_engine) as s:
        s.add(models.Epic(opportunity_id="o1", epic_id="EP-01", name="E", sort_order=1))
        s.flush()
        s.add(models.Br(opportunity_id="o1", br_id="BR-024", epic_id="EP-01", sort_order=1, title="t", statement="s", priority="Must",
                        lifecycle_status="Removed"))
    assert next_id(0) == "BR-025"


def test_a_br_must_belong_to_a_known_epic(app_engine):
    _opportunity(app_engine)
    with pytest.raises(IntegrityError), session_scope(app_engine) as s:
        s.add(models.Br(opportunity_id="o1", br_id="BR-001", epic_id="EP-99", sort_order=1, title="t", statement="s", priority="Must"))


# ---------- import ----------

def _record() -> OpportunityRecord:
    def feature(fid, epic, name, statements, **kw):
        return RecordFeature(id=fid, epic=epic, name=name, description="d", type="crud", priority="Must", complexity="medium", size="M",
                             statement_ids=statements, **kw)

    def br(bid, fid, epic, title, statements, **kw):
        return BusinessRequirement(id=bid, feature_id=fid, title=title, epic=epic, statement=f"{title} statement", statement_ids=statements, **kw)

    return OpportunityRecord(
        version=7, status="APPROVED", opportunity=Opportunity(id="o2", client="Fictional client", project_name="Fictional project",
                                                              commercial_terms={"requested_months": 6}),
        requirements=[RecordRequirement(id="ST-1", text="The system shall do one.", category="functional", citations=[Citation(page=1, section="1 One")]),
                      RecordRequirement(id="ST-2", text="The system shall do two.", category="functional")],
        features=[feature("F-1", "Epic A", "One", ["ST-1"]), feature("F-2", "Epic B", "Two", ["ST-2"], justification="why"),
                  feature("F-3", "Epic A", "Three", [], origin="critic", scope_status="suggested")],
        brd=Brd(version="b3",
                requirements=[br("BR-AA-01", "F-1", "Epic A", "One", ["ST-1"]), br("BR-BB-01", "F-2", "Epic B", "Two", ["ST-2"]),
                              br("BR-AA-02", "F-3", "Epic A", "Three", [], scope_status="suggested")],
                rules=[BrdItem(id="BRULE-01", text="A rule", kind="rule", br_ids=["BR-BB-01"])],
                entities=[BrdItem(id="ENT-01", text="Employee", attributes=["name"])],
                nfrs=[BrdItem(id="NFR-01", text="Fast", measurable=False)],
                constraints=[BrdItem(id="CON-01", text="Six months", kind="schedule")],
                assumptions=[BrdItem(id="ASM-01", text="Cloud", kind="technical")],
                questions=[BrdItem(id="Q-01", text="Which?", kind="conflict", br_ids=["BR-AA-01"]), BrdItem(id="Q-02", text="When?", kind="rule")]),
        estimates={"B": {"total_hours": 999}}, costing={"scenarios": {"B": {"price": 123}}})


DOCS = [{"key": "tor", "name": "tor.pdf", "kind": "tor", "number": "", "date": "", "sha256": "abc", "pages": [{"page": 1, "text": "masked"}]}]


def test_import_moves_the_record_and_renumbers_ids(app_engine):
    decisions = [{"type": "size", "statement_ids": ["ST-1"], "value": "L", "at": "2026-10-30T00:00:00+00:00"}]
    runs = [{"version": 7, "at": "2026-10-30T00:00:00+00:00", "hits": 8, "misses": 0, "live_usd": 0.0}]
    with session_scope(app_engine) as s:
        counts = legacy_import.import_record(s, _record(), DOCS, decisions, runs, code="O2")
    assert counts["brs"] == 3
    with session_scope(app_engine) as s:
        opp = s.get(models.Opportunity, "o2")
        assert (opp.status, opp.code, opp.commercial) == ("BRD Generated", "O2", {"requested_months": 6})
        brs = {b.br_id: b for b in s.scalars(select(models.Br))}
        assert {b: x.legacy_id for b, x in brs.items()} == {"BR-001": "BR-AA-01", "BR-002": "BR-BB-01", "BR-003": "BR-AA-02"}
        assert (brs["BR-003"].epic_id, brs["BR-003"].sort_order, brs["BR-003"].scope_status) == ("EP-01", 2, "suggested")
        assert (brs["BR-002"].epic_id, brs["BR-002"].rationale, brs["BR-002"].lifecycle_status) == ("EP-02", "why", "Active")
        assert {r.status for r in s.scalars(select(models.BrReview))} == {"Pending"}
        assert s.scalars(select(models.BusinessRule)).one().item_id == "BRule-001"
        assert s.scalars(select(models.BusinessRule)).one().br_ids == ["BR-002"]
        q = {x.item_id: x for x in s.scalars(select(models.OpenQuestion))}
        assert (q["OQ-001"].blocking, q["OQ-001"].br_ids, q["OQ-002"].blocking) == (True, ["BR-001"], False)
        assert {c.item_type for c in s.scalars(select(models.ConstraintAssumption))} == {"Constraint", "Assumption"}
        assert s.scalars(select(models.Statement).where(models.Statement.statement_id == "ST-1")).one().section == "1 One"
        assert s.scalars(select(models.Decision)).one().statement_ids == ["ST-1"]
        assert s.scalars(select(models.Run)).one().cache_hits == 8
        assert store.allocate_id(s, "o2", "BR") == "BR-004"
        assert store.allocate_id(s, "o2", "EP", 2) == "EP-03"
        version = s.scalars(select(models.ArtifactVersion)).one()
        assert (version.artifact_type, version.version_label, version.status) == ("BRD", "0.1", "Draft")
        assert s.scalars(select(models.AuditLog)).one().action == "opportunity.imported"


def test_import_carries_no_hours_costs_or_prices(app_engine):
    with session_scope(app_engine) as s:
        legacy_import.import_record(s, _record(), DOCS)
    with session_scope(app_engine) as s:
        snapshot = s.scalars(select(models.ArtifactVersion)).one().snapshot
        assert {"brs", "epics", "open_questions"} <= set(snapshot)
        text_of_snapshot = str(snapshot)
        assert "total_hours" not in text_of_snapshot and "'price'" not in text_of_snapshot


def test_import_twice_is_refused_and_a_failed_import_leaves_nothing(app_engine):
    with session_scope(app_engine) as s:
        legacy_import.import_record(s, _record(), DOCS)
    with pytest.raises(legacy_import.AlreadyImported), session_scope(app_engine) as s:
        legacy_import.import_record(s, _record(), DOCS)
    with session_scope(app_engine) as s:
        assert s.scalar(select(func.count()).select_from(models.Br)) == 3
    broken = _record().model_copy(update={"opportunity": Opportunity(id="o3", client="c", project_name="p")})
    with pytest.raises(ValueError), session_scope(app_engine) as s:
        legacy_import.import_record(s, broken, [], code="O3")
    with session_scope(app_engine) as s:
        assert s.get(models.Opportunity, "o3") is None


def test_pending_priority_becomes_should_with_a_non_blocking_question(app_engine):
    rec = _record()
    rec.features[2].priority = "Should (pending client confirmation)"
    with session_scope(app_engine) as s:
        legacy_import.import_record(s, rec, DOCS)
    with session_scope(app_engine) as s:
        br = s.scalars(select(models.Br).where(models.Br.legacy_id == "BR-AA-02")).one()
        assert (br.priority, br.lifecycle_status, br.ext["feature"]["priority"]) == ("Should", "Active", "Should")
        q = s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.source == "client_confirmation")).one()
        assert (q.item_id, q.blocking, q.status, q.br_ids) == ("OQ-003", False, "Open", ["BR-003"])
        assert legacy_import.normalize_priorities(s, rec.opportunity.id) == {"confirmations": 0, "retired": 0}
        assert all(b["priority"] in ("Must", "Should", "Could") for b in s.scalars(select(models.ArtifactVersion)).one().snapshot["brs"])


def test_wont_becomes_out_of_scope_and_retires_the_id(app_engine):
    rec = _record()
    rec.features[1].priority = "Won't"
    rec.exclusions = [{"text": "Payroll computation is excluded.", "pages": [2], "source": "TOR scope exclusion"}]
    with session_scope(app_engine) as s:
        legacy_import.import_record(s, rec, DOCS)
    with session_scope(app_engine) as s:
        br = s.scalars(select(models.Br).where(models.Br.legacy_id == "BR-BB-01")).one()
        assert (br.br_id, br.lifecycle_status) == ("BR-002", "Removed")
        oos = {x.item_id: x.data for x in s.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.section == "out_of_scope"))}
        assert oos["OOS-001"]["exclusion"] == "Payroll computation is excluded."
        assert (oos["OOS-002"]["exclusion"], oos["OOS-002"]["retired_br"]) == ("Two", "BR-002")
        assert store.allocate_id(s, rec.opportunity.id, "BR") == "BR-004"
        assert legacy_import.normalize_priorities(s, rec.opportunity.id)["retired"] == 0


def test_driver_url_keeps_the_ssl_setting():
    url = "postgresql://u:p@host:5432/dealadvisor?sslmode=require"
    assert driver_url(url) == "postgresql+psycopg://u:p@host:5432/dealadvisor?sslmode=require"
