"""Opportunity record versioning and deterministic feature attributes (no Azure calls)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import determinism, record  # noqa: E402
from app.record import Approval, Opportunity, OpportunityRecord  # noqa: E402
from app.schemas import Feature, Requirement  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(record, "DATA_DIR", tmp_path)


def _rec() -> OpportunityRecord:
    return OpportunityRecord(opportunity=Opportunity(id="test-opp", client="Fictional Co.", project_name="X"))


def test_every_save_creates_a_new_version():
    a = record.save(_rec(), "first")
    b = record.save(a, "edit")
    assert (a.version, b.version) == (1, 2)
    assert record.versions("test-opp") == [1, 2]
    assert record.load("test-opp").change_note == "edit"
    assert record.load("test-opp", 1).change_note == "first"


def test_edit_after_approval_clears_approval():
    a = _rec()
    a.approval = Approval(approved=True, approver="A. Tester", scenario="B", record_version=1)
    approved = record.save(a, "approval: B")
    assert approved.approval.approved
    edited = record.save(approved, "backlog edited")
    assert not edited.approval.approved


def test_record_never_carries_rate_values():
    fields = set(OpportunityRecord.model_fields["costing"].annotation.model_fields)
    assert {"rates_source", "rates_hash"} <= fields
    assert not any("rate" in f and f not in ("rates_source", "rates_hash") for f in fields)


def test_invalid_opportunity_id_rejected():
    with pytest.raises(ValueError):
        record.versions("../etc")


def _f(**kw) -> Feature:
    base = dict(id="x", epic="Leave", name="Leave filing", type="screen_crud", description="d")
    return Feature(**{**base, **kw})


def test_stable_ids_and_order_do_not_depend_on_input_order():
    fs = [_f(epic="B", name="b2"), _f(epic="A", name="a1"), _f(epic="B", name="b1")]
    one = determinism.order_and_id(fs)
    two = determinism.order_and_id(list(reversed(fs)))
    assert [(f.id, f.name) for f in one] == [(f.id, f.name) for f in two]
    assert [f.name for f in one] == ["a1", "b1", "b2"]
    assert one[0].id == determinism.stable_id("A", "a1")


def test_duplicate_epic_and_name_get_suffixed_ids():
    ids = [f.id for f in determinism.order_and_id([_f(), _f()])]
    assert ids[0] != ids[1] and ids[1].endswith("-2")


def test_moscow_priority_rules():
    reqs = {"R1": Requirement(id="R1", text="The System shall compute balances", pages=[4], category="functional"),
            "R2": Requirement(id="R2", text="Users may optionally export to PDF", pages=[7], category="functional")}
    assert determinism.priority(_f(requirement_ids=["R1"]), reqs) == "Must"
    assert determinism.priority(_f(requirement_ids=["R2"]), reqs) == "Could"
    assert determinism.priority(_f(origin="critic"), reqs) == "Should (pending client confirmation)"
    assert determinism.priority(_f(route="out_of_scope"), reqs) == "Won't"


def test_functional_complexity_rules():
    assert determinism.functional_complexity(_f(type="integration", size="M")) == "high"
    assert determinism.functional_complexity(_f(size="XL")) == "high"
    assert determinism.functional_complexity(_f(size="S")) == "low"
    assert determinism.functional_complexity(_f(size="M")) == "medium"
    assert determinism.functional_complexity(_f(size="S", name="Login with MFA")) == "high"
    assert determinism.functional_complexity(_f(size="M", description="Export attendance to payroll")) == "high"
    assert determinism.functional_complexity(_f(size="S"), domain_complexity="high") == "high"


def test_pattern_names_parsed_from_corpus():
    names = [n for n, _ in determinism.pattern_names()]
    assert "Audit trail" in names and len(names) >= 20


def test_rule_questions_vague_integration_and_migration():
    reqs = [Requirement(id="R1", text="The System shall seamlessly address all data privacy needs optimally",
                        pages=[8], section="10.5", category="nfr"),
            Requirement(id="R2", text="Provide attendance data to the payroll system every cut-off", pages=[7],
                        category="functional"),
            Requirement(id="R3", text="Exchange data with the bank using SFTP and a CSV file format", pages=[9],
                        category="functional")]
    feats = [_f(epic="Payroll", name="Payroll feed", type="integration", requirement_ids=["R2"], pages=[7]),
             _f(epic="Payroll", name="Payroll payslips", type="integration", requirement_ids=["R2"], pages=[7]),
             _f(epic="Bank", name="Bank feed", type="integration", requirement_ids=["R3"], pages=[9]),
             _f(epic="Migration", type="data_migration", name="Migrate records", pages=[3])]
    qs = determinism.rule_questions(feats, reqs, [])
    texts = " ".join(q["text"] for q in qs)
    assert "Section 10.5" in texts and "not measurable" in texts
    assert "Payroll feed:" in texts and "Bank feed:" not in texts  # the bank interface names its format
    assert "Migrate records" in texts
    assert "Payroll payslips:" in texts                          # every such integration gets its own question
    again = " ".join(q["text"] for q in determinism.rule_questions(feats, reqs, ["Payroll feed: which system?"]))
    assert "Payroll feed:" not in again and "Payroll payslips:" in again


def test_commercial_terms_reads_the_requested_delivery_period():
    from app.determinism import commercial_terms
    text = "The System shall be delivered, accepted and in live\noperation within five (5) months from the Notice."
    assert commercial_terms(text) == {"requested_months": 5}
    assert commercial_terms("Support continues for twelve (12) months after go-live.") == {}
