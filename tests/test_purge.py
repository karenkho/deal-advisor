"""The admin delete: who may, what is refused, and that everything goes. Drive and LangSmith are mocked. No LLM."""
import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, text
from sqlalchemy.exc import ProgrammingError

from app import brd_service, models, opportunities as ops, purge, record, storage, tracing, workflow as wf
from app.db import session_scope
from app.workflow import Actor
from tests.test_drive_layout import _FakeFiles, _FakeService, _Request
from tests.test_estimate_proposal import _approved_brd, _approved_estimate, _proposal, db  # noqa: F401

ADMIN = Actor("Ada", wf.ADMIN)


@pytest.fixture(autouse=True)
def _no_real_services(monkeypatch, tmp_path):
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path / "store"))
    monkeypatch.setattr(record, "DATA_DIR", tmp_path / "data" / "opportunities")
    monkeypatch.setattr(tracing, "delete_traces", lambda opp_id: {"status": "not_configured", "traces": 0})


@pytest.fixture
def no_protection(monkeypatch):
    """The test opportunities are built from the sample, whose code the real list protects."""
    monkeypatch.setattr(purge, "protected_codes", lambda: set())


def _code(db, opp_id) -> str:
    with session_scope(db) as s:
        return s.get(models.Opportunity, opp_id).code


def _rows_of(db, opp_id) -> dict:
    with db.connect() as conn:
        names = [t for t in sa_inspect(conn).get_table_names() if any(c["name"] == "opportunity_id" for c in sa_inspect(conn).get_columns(t))]
        out = {t: conn.execute(text(f"SELECT count(*) FROM {t} WHERE opportunity_id = :o"), {"o": opp_id}).scalar_one() for t in names}
        out["opportunities"] = conn.execute(text("SELECT count(*) FROM opportunities WHERE id = :o"), {"o": opp_id}).scalar_one()
    return out


def _unchanged(db, opp_id, before) -> None:
    assert _rows_of(db, opp_id) == before


@pytest.mark.parametrize("role", [wf.BDM, wf.SPECIALIST, wf.LEGAL])
def test_a_non_admin_is_refused_and_nothing_goes(db, role):
    opp_id = _approved_brd(db)
    before = _rows_of(db, opp_id)
    with pytest.raises(wf.NotAllowed):
        purge.purge(Actor("Not admin", role), opp_id, _code(db, opp_id), db)
    assert _rows_of(db, opp_id) == {**before, "audit_log": before["audit_log"] + 1}     # the attempt itself is recorded


@pytest.mark.parametrize("code", ["HRIS", "HRIS-04", "hris-04"])
def test_protected_codes_are_refused_even_with_the_right_confirmation(db, code):
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        s.get(models.Opportunity, opp_id).code = code
    before = _rows_of(db, opp_id)
    with pytest.raises(purge.PurgeRefused, match="protected"):
        purge.purge(ADMIN, opp_id, code, db)
    _unchanged(db, opp_id, before)


def test_the_protected_list_comes_from_config():
    assert purge.protected_codes() == {"HRIS", "HRIS-04"}


@pytest.mark.parametrize("typed", ["", "wrong", None])
@pytest.mark.usefixtures("no_protection")
def test_a_wrong_confirmation_is_refused(db, typed):
    opp_id = _approved_brd(db)
    before = _rows_of(db, opp_id)
    with pytest.raises(purge.PurgeRefused, match="does not match"):
        purge.purge(ADMIN, opp_id, typed, db)
    _unchanged(db, opp_id, before)


@pytest.mark.usefixtures("no_protection")
def test_refused_during_a_run_and_allowed_once_it_finishes(db):
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        s.add(models.Run(opportunity_id=opp_id, kind=ops.RUN_PROCESS, status="Running", detail={"instance": ops.INSTANCE}))
    before = _rows_of(db, opp_id)
    with pytest.raises(purge.PurgeRefused, match="in progress"):
        purge.purge(ADMIN, opp_id, _code(db, opp_id), db)
    _unchanged(db, opp_id, before)
    with session_scope(db) as s:
        s.scalars(select(models.Run).where(models.Run.status == "Running")).one().status = "Failed"
    assert purge.purge(ADMIN, opp_id, _code(db, opp_id), db)["database"]["runs"] >= 1


@pytest.mark.usefixtures("no_protection")
def test_everything_goes_across_every_table_the_cache_and_the_store_and_others_stay(db, tmp_path):
    keep = _approved_brd(db)
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        s.add(models.Decision(opportunity_id=opp_id, kind="rename", statement_ids=[], value="x"))
        s.add(models.EditDraft(opportunity_id=opp_id, user_name="Sol", artifact_type="BRD", pending={}))
        opp = s.get(models.Opportunity, opp_id)
        code, name = opp.code, opp.client_name
    cached = record.DATA_DIR / opp_id
    cached.mkdir(parents=True)
    (cached / "v1.json").write_text("{}")
    other_cached = record.DATA_DIR / keep
    other_cached.mkdir()
    (other_cached / "v1.json").write_text("{}")
    root = tmp_path / "store"
    assert any(p.name.startswith(f"{code} - ") for p in root.iterdir())
    kept_before = _rows_of(db, keep)
    before = _rows_of(db, opp_id)
    assert before["audit_log"] and before["runs"] is not None and before["edit_drafts"] == 1 and before["brs"]

    report = purge.purge(ADMIN, opp_id, code, db)

    assert set(_rows_of(db, opp_id).values()) == {0}
    assert {t: n for t, n in report["database"].items() if n} == {t: n for t, n in before.items() if n}     # the counts are real
    assert report["database_rows"] == sum(before.values()) and report["cached_files"] == 1 and report["drive"]["files"] > 0
    assert not cached.exists() and other_cached.exists()
    assert not any(p.name.startswith(f"{code} - ") for p in root.iterdir())
    _unchanged(db, keep, kept_before)
    message = purge.report_text(report)
    assert code in message and "audit_log" in message and "Cached files: 1" in message
    with session_scope(db) as s:
        left = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "opportunity.purged")).one()
        assert left.opportunity_id is None and left.actor_name == ADMIN.name
        words = f"{left.entity_id} {left.details}"
        assert opp_id not in words and code not in words and name not in words     # nothing that names what went


@pytest.mark.usefixtures("no_protection")
def test_a_drive_failure_removes_nothing_from_the_database_and_can_be_retried(db, monkeypatch):
    opp_id = _approved_brd(db)
    before = _rows_of(db, opp_id)

    class Down:
        def purge_folders(self, code):
            raise storage.Unavailable("Drive is not available right now.")
    real = storage.get_store
    monkeypatch.setattr(storage, "get_store", lambda: Down())
    with pytest.raises(purge.PurgeRefused, match="Drive"):
        purge.purge(ADMIN, opp_id, _code(db, opp_id), db)
    _unchanged(db, opp_id, before)
    monkeypatch.setattr(storage, "get_store", real)
    assert purge.purge(ADMIN, opp_id, _code(db, opp_id), db)["database"]["opportunities"] == 1


@pytest.mark.usefixtures("no_protection")
def test_a_trace_failure_also_stops_before_the_database(db, monkeypatch):
    opp_id = _approved_brd(db)
    before = _rows_of(db, opp_id)

    def boom(_):
        raise RuntimeError("no service")
    monkeypatch.setattr(tracing, "delete_traces", boom)
    with pytest.raises(purge.PurgeRefused, match="LangSmith"):
        purge.purge(ADMIN, opp_id, _code(db, opp_id), db)
    _unchanged(db, opp_id, before)


def test_the_app_role_still_cannot_change_audit_rows_directly(db):
    opp_id = _approved_brd(db)
    for statement in ("DELETE FROM audit_log WHERE opportunity_id = :o", "UPDATE audit_log SET action = 'x' WHERE opportunity_id = :o"):
        with pytest.raises(ProgrammingError, match="permission denied"), db.begin() as conn:
            conn.execute(text(statement), {"o": opp_id})
    with pytest.raises(ProgrammingError, match="permission denied"), db.begin() as conn:
        conn.execute(text("TRUNCATE audit_log"))


def test_the_function_is_security_definer_owned_by_the_owner_and_only_the_app_may_run_it(db):
    with db.connect() as conn:
        definer, owner, config = conn.execute(text("SELECT p.prosecdef, r.rolname, p.proconfig FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner WHERE p.proname = 'purge_opportunity'")).one()
        grantees = set(conn.execute(text("SELECT coalesce(r.rolname, 'PUBLIC') FROM pg_proc p, aclexplode(p.proacl) a LEFT JOIN pg_roles r ON r.oid = a.grantee WHERE p.proname = 'purge_opportunity' AND a.privilege_type = 'EXECUTE'")).scalars())
    assert definer and owner == "dealadvisor_owner" and any(c.startswith("search_path=") for c in config)
    assert grantees == {"dealadvisor_owner", "dealadvisor_app"}     # the owner's own, and the app's; not PUBLIC


def test_the_function_refuses_an_unknown_opportunity(db):
    with pytest.raises(Exception, match="Unknown opportunity"), db.begin() as conn:
        conn.execute(text("SELECT purge_opportunity('nope')"))


def test_the_function_rolls_back_as_one_transaction(db):
    opp_id = _approved_brd(db)
    before = _rows_of(db, opp_id)
    with pytest.raises(RuntimeError), session_scope(db) as s:
        s.execute(text("SELECT purge_opportunity(:o)"), {"o": opp_id})
        raise RuntimeError("after the function")
    _unchanged(db, opp_id, before)


# ---------- Drive and LangSmith, mocked ----------

class _DriveFiles(_FakeFiles):
    def __init__(self, rows):
        super().__init__()
        self.rows, self.deleted = rows, []

    def list(self, q, fields, pageSize, pageToken=None):
        parent = q.split("'")[1]
        return _Request({"files": [r for r in self.rows if r["parent"] == parent]})

    def delete(self, fileId):
        self.deleted.append(fileId)
        self.rows = [r for r in self.rows if r["id"] != fileId]
        return _Request({})


def _row(i, name, parent, folder=False):
    return {"id": i, "name": name, "parent": parent, "mimeType": storage.FOLDER_MIME if folder else storage.DOCX_MIME}


def test_drive_folders_are_deleted_permanently_trash_included_and_only_that_code():
    service = _FakeService()
    service._files = _DriveFiles([
        _row("a", "TESTCO - testco", "root", True), _row("b", "BRD", "a", True), _row("c", "BRD-TESTCO-R1-v0.1.docx", "b"), _row("d", "trashed-copy.docx", "b"),
        _row("old", "TESTCO - older name", "root", True), _row("e", "x.docx", "old"),
        _row("o1", "TESTCO-APP - other", "root", True), _row("o2", "keep.docx", "o1"), _row("t", "templates", "root", True), _row("m", "msa.docx", "t")])
    store_ = storage.DriveStore("root", service=service, sleep=lambda _: None)
    assert store_.purge_folders("TESTCO") == {"folders": 3, "files": 3}
    assert set(service._files.deleted) == {"a", "b", "c", "d", "old", "e"}
    assert {r["id"] for r in service._files.rows} == {"o1", "o2", "t", "m"}


def test_trace_delete_without_a_key_checks_nothing(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr("app.tracing.load_dotenv_if_present", lambda: None)
    monkeypatch.setattr("app.tracing.env", lambda name, *a: "")
    assert tracing.delete_traces("opp") == {"status": "not_configured", "traces": 0}


def test_trace_delete_finds_by_metadata_and_posts_the_trace_ids(monkeypatch):
    from types import SimpleNamespace
    import langsmith
    import requests
    monkeypatch.undo()
    posted, asked = [], {}
    monkeypatch.setattr("app.tracing.load_dotenv_if_present", lambda: None)
    monkeypatch.setattr("app.tracing.env", lambda name, *a: {"LANGSMITH_API_KEY": "k", "LANGSMITH_PROJECT": "p"}.get(name, ""))

    class Client:
        api_url = "https://api.example.test"

        def list_runs(self, **kwargs):
            asked.update(kwargs)
            return [SimpleNamespace(trace_id="t1", session_id="s"), SimpleNamespace(trace_id="t1", session_id="s"), SimpleNamespace(trace_id="t2", session_id="s")]
    monkeypatch.setattr(langsmith, "Client", Client)
    monkeypatch.setattr(requests, "post", lambda url, headers, json, timeout: posted.append((url, json)) or SimpleNamespace(raise_for_status=lambda: None))
    assert tracing.delete_traces("opp-1") == {"status": "deleted", "traces": 2}
    assert 'eq(metadata_key, "opportunity_id")' in asked["filter"] and 'eq(metadata_value, "opp-1")' in asked["filter"] and asked["project_name"] == "p"
    assert posted == [("https://api.example.test/api/v1/runs/delete", {"session_id": "s", "trace_ids": ["t1", "t2"]})]




def test_a_refused_attempt_at_a_gate_is_recorded_even_though_it_changes_nothing(db):
    opp_id = _approved_brd(db)
    with pytest.raises(wf.NotAllowed), session_scope(db) as s:
        brd_service.approve(s, Actor("Bea", wf.BDM), opp_id)
    with session_scope(db) as s:
        row = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "permission.refused")).one()
        assert (row.opportunity_id, row.actor_name, row.details["action"]) == (opp_id, "Bea", wf.ACT_VALIDATE_BRD)
