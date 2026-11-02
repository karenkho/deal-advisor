"""Removing questions and BRD items by re-upload: seen in the diff, confirmed, Withdrawn on apply and never deleted. No LLM."""
import io

import pytest
from docx import Document
from sqlalchemy import select

from app import brd_compare, brd_docx, brd_service as svc, models, proposal_service as prop, store
from app.db import session_scope
from tests.test_workflow import SS, _apply, _approve, _docx, _edit, _seed, _status, _text_of, _version, db, migrated  # noqa: F401
from app import workflow as wf


def _table(doc, first: str, second: str | None = None):
    return next(t for t in doc.tables if t.rows[0].cells[0].text == first and (second is None or t.rows[0].cells[1].text.startswith(second)))


def _drop(doc, first: str, item_id: str, second: str | None = None) -> None:
    table = _table(doc, first, second)
    row = next(r for r in table.rows if r.cells[0].text == item_id)
    table._tbl.remove(row._tr)


def _question(db, item_id: str) -> models.OpenQuestion:
    with session_scope(db) as s:
        row = s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.item_id == item_id)).one()
        s.expunge(row)
        return row


def _rows(table) -> dict:
    return {r.cells[0].text: r.cells[1].text for r in table.rows[1:]}


def _seed_risk(db, opp_id: str) -> None:
    with session_scope(db) as s:
        if s.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.section == "risk")).first() is None:
            s.add(models.BrdSectionItem(opportunity_id=opp_id, section="risk", item_id=store.allocate_id(s, opp_id, "R"), sort_order=1,
                                        data={"description": "Data may arrive late", "probability": "Med", "impact": "Med", "mitigation": "Plan early", "owner": "PM"}))
            s.flush()
            version = s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id)).one()
            version.snapshot = store.brd_snapshot(s, opp_id)


def test_removing_a_question_shows_in_the_diff_and_changes_nothing_until_applied(db):
    opp_id = _seed(db)
    with session_scope(db) as s:
        result = brd_docx.diff(s, opp_id, _edit(_docx(db, opp_id), lambda doc, t: _drop(doc, "Question ID", "OQ-001")))
    assert [(w["item_id"], w["section"]) for w in result["changes"]["withdrawn"]] == [("OQ-001", "open_question")]
    assert svc.has_changes(result["changes"]) and _question(db, "OQ-001").withdrawn is None
    assert not any("OQ-001" in line for d in result["text_diffs"] for line in d["diff"])     # not repeated as a change that is ignored


def test_applying_marks_withdrawn_with_who_when_why_and_keeps_the_row(db):
    opp_id = _seed(db)
    with session_scope(db) as s:
        result = brd_docx.diff(s, opp_id, _edit(_docx(db, opp_id), lambda doc, t: _drop(doc, "Question ID", "OQ-001")))
    result["changes"]["withdrawn"][0]["reason"] = "Answered on a call"
    with session_scope(db) as s:
        version = svc.apply_changes(s, SS, opp_id, result["changes"], "re-upload")
        snapshot = version.snapshot
        assert "withdrawn" in version.change_summary and "OQ-001" in version.change_summary and "order only" not in version.change_summary
        entry = s.scalars(select(models.AuditLog).where(models.AuditLog.action == "item.withdrawn")).one()
        assert (entry.entity_id, entry.actor_name, entry.details["reason"]) == ("OQ-001", SS.name, "Answered on a call")
    q = _question(db, "OQ-001")
    assert q.status == "Withdrawn" and q.withdrawn["by"] == SS.name and q.withdrawn["role"] == SS.role and q.withdrawn["reason"] == "Answered on a call" and q.withdrawn["at"]
    assert "OQ-001" not in {r["item_id"] for r in snapshot["open_questions"]} and [w["item_id"] for w in snapshot["withdrawn"]] == ["OQ-001"]
    assert snapshot["stability"]["passed"]


@pytest.mark.parametrize("section,first,second,prefix", [("in_scope", "ID", "Capability", "IS-001"), ("objective", "Objective ID", None, "BO-001"), ("risk", "Risk ID", None, "R-001")])
def test_removing_a_scope_objective_or_risk_item_withdraws_only_it(db, section, first, second, prefix):
    opp_id = _seed(db)
    _seed_risk(db, opp_id)
    with session_scope(db) as s:
        before = len(s.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.section == section)).all())
    result, label = _apply(db, opp_id, _edit(_docx(db, opp_id), lambda doc, t: _drop(doc, first, prefix, second)))
    assert [(w["item_id"], w["section"]) for w in result["changes"]["withdrawn"]] == [(prefix, section)] and label == "0.2"
    with session_scope(db) as s:
        rows = s.scalars(select(models.BrdSectionItem).where(models.BrdSectionItem.section == section)).all()
        assert len(rows) == before and next(r for r in rows if r.item_id == prefix).withdrawn["by"] == SS.name    # kept, not deleted
    table = _rows(_table(Document(io.BytesIO(_docx(db, opp_id))), first, second))
    assert prefix not in table


def test_withdrawn_items_stay_out_of_later_versions_documents_and_diffs(db):
    opp_id = _seed(db)
    text = _question(db, "OQ-001").text
    _apply(db, opp_id, _edit(_docx(db, opp_id), lambda doc, t: _drop(doc, "Question ID", "OQ-001")))
    assert text not in _text_of(_docx(db, opp_id))
    with session_scope(db) as s:
        assert brd_docx.diff(s, opp_id, _docx(db, opp_id))["changes"]["withdrawn"] == []     # the next clean file does not withdraw it again
        assert text not in " ".join(prop.question_lists(s, opp_id)["client"] + prop.question_lists(s, opp_id)["internal"])
    _approve(db, opp_id)     # a new version, a new snapshot: still out
    assert text not in _text_of(_docx(db, opp_id))
    with session_scope(db) as s:
        latest = store.brd_snapshot(s, opp_id)
        assert "OQ-001" not in {q["item_id"] for q in latest["open_questions"]} and "OQ-001" in {w["item_id"] for w in latest["withdrawn"]}


def test_the_stability_check_expects_a_listed_withdrawal_and_blocks_an_unlisted_one(db):
    opp_id = _seed(db)
    with session_scope(db) as s:
        s.scalars(select(models.OpenQuestion).where(models.OpenQuestion.item_id == "OQ-001")).one().withdrawn = {"by": "x", "role": "y", "at": "z", "reason": None}
        s.flush()
        changes = svc.empty_changes()
        changes["modified"] = [{"br_id": "BR-001", "changes": {"title": "Changed"}}]
        with pytest.raises(svc.UnexpectedChange, match="OQ-001"):
            svc.apply_changes(s, SS, opp_id, changes, "re-upload")
        s.rollback()


def test_compare_labels_a_withdrawn_item_as_withdrawn(db):
    opp_id = _seed(db)
    _apply(db, opp_id, _edit(_docx(db, opp_id), lambda doc, t: _drop(doc, "Question ID", "OQ-001")))
    with session_scope(db) as s:
        report = brd_compare.compare(s, opp_id, "0.1", "0.2")
    assert next(i for i in report["items"] if i["id"] == "OQ-001")["status"] == "Withdrawn"


def test_remove_and_re_add_reinstates_the_same_id_and_it_returns_to_documents(db):
    opp_id = _seed(db)
    question = _question(db, "OQ-001")
    was_status = question.status
    _apply(db, opp_id, _edit(_docx(db, opp_id), lambda doc, t: _drop(doc, "Question ID", "OQ-001")))

    def re_add(doc, t):
        row = _table(doc, "Question ID").add_row().cells
        row[0].text, row[1].text, row[4].text = "OQ-001", question.text, "Open"

    result, label = _apply(db, opp_id, _edit(_docx(db, opp_id), re_add))
    assert [r["item_id"] for r in result["changes"]["reinstated"]] == ["OQ-001"] and result["changes"]["withdrawn"] == [] and label == "0.3"
    q = _question(db, "OQ-001")
    assert q.withdrawn is None and q.status == was_status and question.text in _text_of(_docx(db, opp_id))
    with session_scope(db) as s:
        assert s.scalars(select(models.AuditLog).where(models.AuditLog.action == "item.reinstated")).one().entity_id == "OQ-001"


def test_a_renumbered_file_is_matched_by_text_and_the_missing_rows_are_withdrawn(db):
    """The real failure: rows deleted in the file, then the IDs renumbered 1..n, which used to overwrite each question with another's text."""
    opp_id = _seed(db)
    with session_scope(db) as s:
        for n in (2, 3, 4):
            item_id = store.allocate_id(s, opp_id, "OQ")
            s.add(models.OpenQuestion(opportunity_id=opp_id, item_id=item_id, sort_order=100 + n, text=f"Extra question number {n}?", blocking=False))
        s.flush()
        s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id)).one().snapshot = store.brd_snapshot(s, opp_id)
        before = {q.item_id: q.text for q in s.scalars(select(models.OpenQuestion))}

    def renumber(doc, t):
        table = _table(doc, "Question ID")
        ids = [r.cells[0].text for r in table.rows[1:]]
        table._tbl.remove(table.rows[2]._tr)     # the second question goes
        for row, item_id in zip(table.rows[1:], ids):     # and the IDs close the gap
            row.cells[0].text = item_id

    result, _ = _apply(db, opp_id, _edit(_docx(db, opp_id), renumber))
    assert result["renumbered"] and not result["changes"]["questions"]
    assert len(result["changes"]["withdrawn"]) == 1
    with session_scope(db) as s:
        after = {q.item_id: q.text for q in s.scalars(select(models.OpenQuestion))}
    assert after == before     # no question took another's text


def test_a_repeated_id_and_a_missing_table_are_refused_and_change_nothing(db):
    opp_id = _seed(db)

    def repeat(doc, t):
        table = _table(doc, "Question ID")
        table.add_row().cells[0].text = table.rows[1].cells[0].text
        table.rows[-1].cells[1].text = "Something else entirely"

    def missing(doc, t):
        table = _table(doc, "Question ID")
        table._tbl.getparent().remove(table._tbl)

    with session_scope(db) as s:
        with pytest.raises(brd_docx.BrdFileError, match="appears twice"):
            brd_docx.diff(s, opp_id, _edit(_docx(db, opp_id), repeat))
        with pytest.raises(brd_docx.BrdFileError, match="table is missing"):
            brd_docx.diff(s, opp_id, _edit(_docx(db, opp_id), missing))


def test_an_emptied_row_counts_as_removed(db):
    opp_id = _seed(db)

    def empty(doc, t):
        _table(doc, "Question ID").rows[1].cells[1].text = ""

    with session_scope(db) as s:
        result = brd_docx.diff(s, opp_id, _edit(_docx(db, opp_id), empty))
    assert [w["item_id"] for w in result["changes"]["withdrawn"]] == ["OQ-001"]


def test_after_approval_a_withdrawal_goes_through_a_change_request(db):
    opp_id = _seed(db)
    _approve(db, opp_id)
    assert _status(db, opp_id) == wf.BRD_APPROVED and _version(db, opp_id) == "1.0"
    _, label = _apply(db, opp_id, _edit(_docx(db, opp_id), lambda doc, t: _drop(doc, "Question ID", "OQ-001")))
    assert label == "1.1" and _status(db, opp_id) == wf.BRD_VALIDATION
    with session_scope(db) as s:
        request = s.scalars(select(models.ChangeRequest)).one()
        assert request.status == "Confirmed" and [w["item_id"] for w in request.diff_summary["changes"]["withdrawn"]] == ["OQ-001"]


def _diff_screen():
    import streamlit as st

    from app import workspace_ui as ui
    ui._show_diff(st.session_state["opp_id"], st.session_state["result"])


def test_the_diff_screen_lists_removed_items_as_removed_and_says_when_ids_were_renumbered(db, migrated, monkeypatch):
    from streamlit.testing.v1 import AppTest
    opp_id = _seed(db)
    monkeypatch.setenv("DATABASE_URL", migrated["app"])
    with session_scope(db) as s:
        result = brd_docx.diff(s, opp_id, _edit(_docx(db, opp_id), lambda doc, t: _drop(doc, "Question ID", "OQ-001")))
    result["renumbered"] = [{"file_id": "OQ-002", "item_id": "OQ-004"}]
    at = AppTest.from_function(_diff_screen, default_timeout=30)
    at.session_state["opp_id"], at.session_state["result"] = opp_id, result
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    shown = " ".join(m.value for m in at.markdown) + " ".join(w.value for w in at.warning)
    assert "Removed from the file" in shown and "OQ-002 in the file is OQ-004" in shown
    frame = at.table[0].value
    assert list(frame.index) == ["OQ-001"] and frame.iloc[0]["Change"] == "removed"
