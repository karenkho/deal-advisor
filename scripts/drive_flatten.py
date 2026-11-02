"""Moves every opportunity's Drive files to the flat layout (1 Nov 2026). Same file IDs, so stored links keep working. Run once, safe to repeat:
    python scripts/drive_flatten.py          # shows the plan, changes nothing
    python scripts/drive_flatten.py --apply  # moves, renames, stamps, then trashes the emptied folders
Files nothing here recognises, such as a person's own copy, move with their folder and keep their name. Nothing is ever hard-deleted: duplicates go to Trash."""
from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docx import Document  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import models, opportunities, proposal_service, publishing, storage, store  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.workflow import SYSTEM_ACTOR  # noqa: E402

PRIORITY = {"versions": 0, "archive": 1, "current": 2}     # stored links point at the versions copy, so that file is the one kept
_CLIENT = re.compile(r"^(PROPOSAL-.+-R1-v\d+\.\d+)(-SUBMITTED)?\.docx$")
_ESTIMATE = re.compile(r"^(ESTIMATE-.+-R1-v\d+\.\d+)\.docx$")


def new_name(name: str) -> str:
    client = _CLIENT.match(name)
    if client and not name.endswith("-INTERNAL.docx") and "-CLIENT" not in name:
        return f"{client.group(1)}-CLIENT{client.group(2) or ''}.docx"
    estimate = _ESTIMATE.match(name)
    return f"{estimate.group(1)}-INTERNAL.docx" if estimate and not name.endswith("-INTERNAL.docx") else name


def plan(tree: dict[str, dict[str, list[dict]]]) -> dict:
    """tree: top folder -> subfolder ("" for files directly in it) -> files. Pure, so it can be tested without Drive. BRD files whose name is repeated across
    versions, archive and current become one: the versions copy, with the archive copy's content, because that is the one stamped SUPERSEDED."""
    moves, trash, content_from = [], [], []
    brd = tree.get("BRD", {})
    by_name: dict[str, list[tuple[str, dict]]] = {}
    for sub, files in brd.items():
        for f in files:
            by_name.setdefault(f["name"], []).append((sub, f))
    for name, copies in sorted(by_name.items()):
        copies.sort(key=lambda c: PRIORITY.get(c[0], -1))
        keep_sub, keep = copies[0]
        if keep_sub:
            moves.append({"id": keep["id"], "name": name, "to": "BRD", "new_name": name, "from": f"BRD/{keep_sub}", "stamp": None})
        trash += [f for _, f in copies[1:]]
        archived = next((f for sub, f in copies[1:] if sub == "archive"), None)
        if archived:
            content_from.append({"id": keep["id"], "source": archived["id"], "name": name})
    for f in tree.get("estimate", {}).get("internal", []) + tree.get("estimate", {}).get("", []):
        where = "estimate/internal" if f in tree.get("estimate", {}).get("internal", []) else "estimate"
        if where != "estimate" or new_name(f["name"]) != f["name"]:
            moves.append({"id": f["id"], "name": f["name"], "to": "estimate", "new_name": new_name(f["name"]), "from": where, "stamp": None})
    for f in tree.get("proposal", {}).get("internal", []):
        moves.append({"id": f["id"], "name": f["name"], "to": "proposal", "new_name": f["name"], "from": "proposal/internal", "stamp": proposal_service.INTERNAL_MARK})
    for f in tree.get("proposal", {}).get("", []):
        if new_name(f["name"]) != f["name"]:
            moves.append({"id": f["id"], "name": f["name"], "to": "proposal", "new_name": new_name(f["name"]), "from": "proposal", "stamp": None})
    emptied = [("BRD", s) for s in brd if s] + [(top, "internal") for top in ("estimate", "proposal") if "internal" in tree.get(top, {})]
    return {"moves": moves, "trash": trash, "content_from": content_from, "emptied": emptied}


def _tree(st, folder_id: str) -> tuple[dict, dict]:
    tree, ids = {}, {}
    for top in st._children(folder_id):
        if top["mimeType"] != storage.FOLDER_MIME:
            continue
        ids[top["name"]] = top["id"]
        tree[top["name"]] = {"": []}
        for child in st._children(top["id"]):
            if child["mimeType"] == storage.FOLDER_MIME:
                ids[f"{top['name']}/{child['name']}"] = child["id"]
                tree[top["name"]][child["name"]] = [f for f in st._children(child["id"]) if f["mimeType"] != storage.FOLDER_MIME]
            else:
                tree[top["name"]][""].append(child)
    return tree, ids


def _download(st, file_id: str) -> bytes:
    return st._call(lambda: st.service.files().get_media(fileId=file_id))


def _upload(st, file_id: str, data: bytes) -> None:
    from googleapiclient.http import MediaIoBaseUpload
    st._call(lambda: st.service.files().update(fileId=file_id, media_body=MediaIoBaseUpload(io.BytesIO(data), mimetype=storage.DOCX_MIME, resumable=False), fields="id"))


def _stamped(data: bytes, mark: str) -> bytes:
    doc = Document(io.BytesIO(data))
    publishing.add_to_header(doc, mark, replace=False)
    for section in doc.sections:     # the old wording goes; the status stamp stays
        for paragraph in section.header.paragraphs:
            if paragraph.text == "INTERNAL REVIEW COPY — NOT FOR THE CLIENT":
                paragraph.text = ""
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _backfill(session, st, opp: models.Opportunity) -> list[str]:
    """Any current proposal's two copies, which earlier layouts did not all publish. Rendered from the database: no LLM."""
    version = opportunities.current_version(session, opp.id, "PROPOSAL")
    if version is None or version.status == "Superseded":
        return []
    done = []
    for parts, name, data in publishing._files(session, opp, _with_status_cleared(version)):
        if not any(f["name"] == name for f in st.list_files(parts)):
            st.put(parts, name, data)
            done.append(name)
    return done


def _with_status_cleared(version):
    """_files leaves out what a published approved version already has: here it must look at both copies."""
    version.publish_status = None
    return version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    apply = parser.parse_args().apply
    st = storage.get_store()
    with session_scope() as session:
        opps = {publishing.folder_name(o): o for o in session.scalars(select(models.Opportunity))}
        for folder in st._children(st.root_id):
            if folder["mimeType"] != storage.FOLDER_MIME or folder["name"] == storage.TEMPLATES_FOLDER:
                continue
            tree, ids = _tree(st, folder["id"])
            steps = plan(tree)
            print(f"\n{folder['name'].split(' - ')[0]}: {len(steps['moves'])} moved or renamed, {len(steps['trash'])} duplicates to Trash, {len(steps['emptied'])} folders to Trash")
            for m in steps["moves"]:
                print(f"  {m['from']}/{m['name']} -> {m['to']}/{m['new_name']}" + (" (stamped)" if m["stamp"] else ""))
            if not apply:
                continue
            for c in steps["content_from"]:
                _upload(st, c["id"], _download(st, c["source"]))
            for m in steps["moves"]:
                if m["stamp"]:
                    stamped = _stamped(_download(st, m["id"]), m["stamp"])
                    _upload(st, m["id"], stamped)
                    for row in session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.drive_file_id == m["id"])):
                        row.file_hash = opportunities.sha256(stamped)
                source, target = ids[m["from"]], ids[m["to"]]
                st._call(lambda m=m, source=source, target=target: st.service.files().update(fileId=m["id"], addParents=target, removeParents=source, body={"name": m["new_name"]}, fields="id"))
            for f in steps["trash"]:
                st._call(lambda f=f: st.service.files().update(fileId=f["id"], body={"trashed": True}, fields="id"))
            for top, sub in steps["emptied"]:
                folder_id = ids[f"{top}/{sub}"]
                left = st._children(folder_id)
                if left:
                    print(f"  left in place, not empty: {top}/{sub} ({len(left)} files)")
                    continue
                st._call(lambda folder_id=folder_id: st.service.files().update(fileId=folder_id, body={"trashed": True}, fields="id"))
            st._folders.clear()
            opp = opps.get(folder["name"])
            backfilled = []
            if opp is not None:
                status = None
                version = opportunities.current_version(session, opp.id, "PROPOSAL")
                status = version.publish_status if version else None
                backfilled = _backfill(session, st, opp)
                if version is not None:
                    version.publish_status = status
                store.audit(session, SYSTEM_ACTOR.name, SYSTEM_ACTOR.role, "drive.layout_flattened", opportunity_id=opp.id, details={"moved": len(steps["moves"]), "duplicates_trashed": len(steps["trash"]), "backfilled": backfilled})
            print(f"  added: {backfilled}")
    print("\nPlan only: nothing changed." if not apply else "\nDone.")


if __name__ == "__main__":
    main()
