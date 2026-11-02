"""The flat Drive layout: where each file goes, how it is stamped, and the one-off move of existing files. No LLM, no Drive."""
import importlib.util
import sys
from pathlib import Path

from docx import Document

from app import brd_service as svc, models, publishing, storage
from app.db import session_scope
from tests.test_contracts import _tree
from tests.test_estimate_proposal import SS, _approved_brd, _approved_estimate, _proposal, db  # noqa: F401

_spec = importlib.util.spec_from_file_location("drive_flatten", Path(__file__).resolve().parents[1] / "scripts" / "drive_flatten.py")
flatten = importlib.util.module_from_spec(_spec)
sys.modules["drive_flatten"] = flatten
_spec.loader.exec_module(flatten)


def _header(path) -> str:
    return " ".join(p.text for section in Document(path).sections for p in section.header.paragraphs)


def test_folders_are_flat_with_the_copy_type_in_the_name_and_both_proposal_drafts(db, tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(root))
    opp_id = _approved_estimate(db)
    _proposal(db, opp_id)
    with session_scope(db) as s:
        opp = s.get(models.Opportunity, opp_id)
        folder, code = publishing.folder_name(opp), opp.code
    tree = _tree(root)
    assert not any("/internal/" in t or "/versions/" in t or "/current/" in t or "/archive/" in t for t in tree)
    client = root / folder / "proposal" / f"PROPOSAL-{code}-R1-v0.1-CLIENT.docx"
    internal = root / folder / "proposal" / f"PROPOSAL-{code}-R1-v0.1-INTERNAL.docx"
    estimate = root / folder / "estimate" / f"ESTIMATE-{code}-R1-v1.0-INTERNAL.docx"
    assert client.exists() and internal.exists() and estimate.exists()
    assert "INTERNAL — NOT FOR CLIENT" in _header(internal) and "DRAFT" in _header(internal)
    assert "INTERNAL" not in _header(client)
    assert "INTERNAL — CONTAINS PRICING" in _header(estimate)


def test_the_internal_stamp_is_on_every_page_whatever_the_sections(tmp_path):
    doc = Document()
    doc.add_section()
    doc.sections[1].different_first_page_header_footer = True
    publishing.add_to_header(doc, "INTERNAL — NOT FOR CLIENT", replace=True)
    for section in doc.sections:
        assert "INTERNAL — NOT FOR CLIENT" in section.header.paragraphs[0].text
    assert "INTERNAL — NOT FOR CLIENT" in doc.sections[1].first_page_header.paragraphs[0].text


def test_an_earlier_approved_brd_is_restamped_in_place_and_the_new_one_reads_approved(db, tmp_path, monkeypatch):
    root = tmp_path / "store"
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(root))
    opp_id = _approved_brd(db)
    with session_scope(db) as s:
        svc.retire_br(s, SS, opp_id, "BR-002")
        svc.accept_all_remaining(s, SS, opp_id)
        svc.approve(s, SS, opp_id)
        opp = s.get(models.Opportunity, opp_id)
        folder, code = publishing.folder_name(opp), opp.code
    assert "SUPERSEDED" in _header(root / folder / "BRD" / f"BRD-{code}-R1-v1.0.docx")
    assert "APPROVED" in _header(root / folder / "BRD" / f"BRD-{code}-R1-v2.0.docx")
    assert (root / folder / "BRD" / f"BRD-{code}-R1-compare-v1.0-v2.0.docx").exists()


def _file(i: str, name: str) -> dict:
    return {"id": i, "name": name}


def test_the_move_plan_keeps_stored_links_stamps_internal_copies_and_leaves_a_persons_copy_alone():
    steps = flatten.plan({
        "BRD": {"": [], "versions": [_file("v1", "BRD-X-R1-v1.0.docx"), _file("v2", "BRD-X-R1-v2.0.docx"), _file("mine", "Copy of BRD-X-R1-v1.0.docx")],
                "current": [_file("c2", "BRD-X-R1-v2.0.docx")], "archive": [_file("a1", "BRD-X-R1-v1.0.docx")]},
        "estimate": {"": [], "internal": [_file("e", "ESTIMATE-X-R1-v1.0.docx")]},
        "proposal": {"": [_file("p", "PROPOSAL-X-R1-v1.0.docx"), _file("sub", "PROPOSAL-X-R1-v1.0-SUBMITTED.docx")], "internal": [_file("i", "PROPOSAL-X-R1-v1.0-INTERNAL.docx")]},
        "contracts": {"": [_file("s", "SOW-X-R1-v1.0.docx")]}})
    moved = {m["id"]: m for m in steps["moves"]}
    assert {m["to"] for m in steps["moves"] if m["id"] in ("v1", "v2", "mine")} == {"BRD"} and moved["mine"]["new_name"] == "Copy of BRD-X-R1-v1.0.docx"
    assert {f["id"] for f in steps["trash"]} == {"c2", "a1"}     # the duplicates only
    assert steps["content_from"] == [{"id": "v1", "source": "a1", "name": "BRD-X-R1-v1.0.docx"}]     # the copy stamped SUPERSEDED
    assert moved["e"]["to"] == "estimate" and moved["e"]["new_name"] == "ESTIMATE-X-R1-v1.0-INTERNAL.docx"
    assert moved["i"]["to"] == "proposal" and moved["i"]["stamp"] == "INTERNAL — NOT FOR CLIENT"
    assert moved["p"]["new_name"] == "PROPOSAL-X-R1-v1.0-CLIENT.docx" and moved["sub"]["new_name"] == "PROPOSAL-X-R1-v1.0-CLIENT-SUBMITTED.docx"
    assert "s" not in moved
    assert set(steps["emptied"]) == {("BRD", "versions"), ("BRD", "current"), ("BRD", "archive"), ("estimate", "internal"), ("proposal", "internal")}


def test_the_move_plan_is_safe_to_repeat():
    steps = flatten.plan({"BRD": {"": [_file("v1", "BRD-X-R1-v1.0.docx")]}, "estimate": {"": [_file("e", "ESTIMATE-X-R1-v1.0-INTERNAL.docx")]},
                          "proposal": {"": [_file("p", "PROPOSAL-X-R1-v1.0-CLIENT.docx"), _file("i", "PROPOSAL-X-R1-v1.0-INTERNAL.docx"), _file("s", "PROPOSAL-X-R1-v1.0-CLIENT-SUBMITTED.docx")]}})
    assert steps == {"moves": [], "trash": [], "content_from": [], "emptied": []}


class _Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class _FakeFiles:
    """Enough of the Drive files API for put: list, create and update."""

    def __init__(self):
        self.rows, self.updates = [], 0

    def list(self, q, fields, pageSize):
        parent = q.split("'")[1]
        name = q.split("name='")[1].split("'")[0] if "name='" in q else None
        return _Request({"files": [r for r in self.rows if r["parent"] == parent and (name is None or r["name"] == name)]})

    def create(self, body, media_body=None, fields=None):
        row = {"id": f"id-{len(self.rows)}", "name": body["name"], "parent": body["parents"][0], "mimeType": body.get("mimeType", storage.DOCX_MIME), "modifiedTime": "t", "webViewLink": "link"}
        self.rows.append(row)
        return _Request(row)

    def update(self, fileId, media_body=None, fields=None):
        self.updates += 1
        return _Request(next(r for r in self.rows if r["id"] == fileId))


class _FakeService:
    def __init__(self):
        self._files = _FakeFiles()

    def files(self):
        return self._files


def test_publishing_the_same_name_again_replaces_the_file_and_keeps_its_link():
    service = _FakeService()
    store_ = storage.DriveStore("root", service=service, sleep=lambda _: None)
    first = store_.put(["O", "proposal"], "PROPOSAL-X-R1-v0.1-CLIENT.docx", b"one")
    second = store_.put(["O", "proposal"], "PROPOSAL-X-R1-v0.1-CLIENT.docx", b"two")
    assert first["file_id"] == second["file_id"] and service.files().updates == 1
    assert len([r for r in service.files().rows if r["name"].endswith(".docx")]) == 1
