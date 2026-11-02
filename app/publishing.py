"""Publishing to Drive or the local folder (POC v3 W9). Flat folders per opportunity, under the root: inputs/, BRD/, estimate/, proposal/, contracts/. The copy type is in
the file name (-CLIENT, -INTERNAL), so a folder holds both: share single client files, never a folder. Each file holds its revision history and a status stamp; each
publish records its location and hash on the version and an audit entry. If the store is unavailable, the version is marked pending and offered for retry: the workflow continues, because the database is authoritative."""
from __future__ import annotations

import io

from docx import Document
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import tracing, brd_compare, brd_docx, contract_service, estimate_service, models, opportunities, proposal_service, storage, store

PENDING, AUTH_EXPIRED, PUBLISHED = "pending", "auth_expired", "published"
TEXT_MIME = "text/plain"


def folder_name(opp: models.Opportunity) -> str:
    return f"{opp.code} - {opp.client_name}"


def _versions(session: Session, opp_id: str, artifact: str) -> list[models.ArtifactVersion]:
    return session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type == artifact)
                           .order_by(models.ArtifactVersion.version_major, models.ArtifactVersion.version_minor)).all()


def stamp_document(data: bytes, stamp: str, revisions: list[tuple[str, str, str]]) -> bytes:
    """The status at the top of every page and the revision history at the end. It relies on no style existing and changes none, because the file may be the company's legal template."""
    doc = Document(io.BytesIO(data))
    add_to_header(doc, stamp)
    doc.add_paragraph().add_run("Revision history").bold = True
    table = doc.add_table(rows=1, cols=3)
    for cell, head in zip(table.rows[0].cells, ("Version", "Date", "Status")):
        cell.text = head
    for revision in revisions:
        for cell, value in zip(table.add_row().cells, revision):
            cell.text = value
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _stamped(data: bytes, session: Session, version: models.ArtifactVersion, stamp: str) -> bytes:
    """Dates and status only in the revision history, because some of these go to the client."""
    rows = [(v.version_label, v.created_at.date().isoformat(), v.status) for v in _versions(session, version.opportunity_id, version.artifact_type)
            if (v.version_major, v.version_minor) <= (version.version_major, version.version_minor)]
    return stamp_document(data, stamp, rows)


def add_to_header(doc, text: str, replace: bool = False) -> None:
    """On every page, whatever the section or first-page setup. It relies on no style existing, because the file may be the company's legal template."""
    for section in doc.sections:
        section.header.is_linked_to_previous = False
        headers = [section.header] + ([section.first_page_header] if section.different_first_page_header_footer else [])
        for header in headers:
            if replace:
                for paragraph in header.paragraphs:
                    paragraph.text = ""
                header.paragraphs[0].add_run(text).bold = True
            else:
                header.add_paragraph().add_run(text).bold = True


def _stamp_of(version: models.ArtifactVersion) -> str:
    return {"Approved": "APPROVED", "Superseded": "SUPERSEDED"}.get(version.status, "DRAFT")


def _files(session: Session, opp: models.Opportunity, version: models.ArtifactVersion) -> list[tuple[list[str], str, bytes]]:
    """(folder parts, file name, content) for one version. The MSA goes in the private contracts folder only."""
    root, code, label, kind = [folder_name(opp)], opp.code, version.version_label, version.artifact_type
    stamp = _stamp_of(version)
    if kind == "BRD":
        data = brd_docx.render(session, opp.id, version)
        name = brd_docx.file_name(code, label)
        out = [(root + ["BRD"], name, data)]
        earlier = [v for v in _versions(session, opp.id, "BRD") if v.status == "Superseded" and v.version_minor == 0 and v.version_major >= 1 and v.version_major < version.version_major]
        if earlier:
            previous = earlier[-1]
            out.append((root + ["BRD"], f"BRD-{code}-R1-compare-v{previous.version_label}-v{label}.docx", brd_compare.report_docx(session, opp.id, brd_compare.compare(session, opp.id, previous.version_label, label))))
        return out
    if kind == "ESTIMATE":
        return [(root + ["estimate"], estimate_service.file_name(code, label), _stamped(estimate_service.summary_docx(session, opp.id, version), session, version, stamp))]
    if kind == "PROPOSAL":
        internal = (root + ["proposal"], proposal_service.file_name(code, label, True), _stamped(proposal_service.internal_copy(session, opp.id, version), session, version, stamp))
        client = (root + ["proposal"], proposal_service.file_name(code, label), _stamped(proposal_service.client_copy(session, opp.id, version), session, version, stamp))
        if version.status != "Approved":     # drafts of both; a client draft that fails the leak check stays off Drive
            return [internal] + ([] if version.snapshot["checks"]["leak_hits"] else [client])
        out = [] if version.publish_status == PUBLISHED else [internal, client]
        if version.snapshot.get("submitted_on"):     # the copy that went to the client, marked as such
            out.append((root + ["proposal"], proposal_service.file_name(code, label, submitted=True), _stamped(proposal_service.client_copy(session, opp.id, version), session, version, "SUBMITTED")))
        return out
    if kind == "SOW":
        return [(root + ["contracts"], f"SOW-{code}-R1-v{label}.docx", _stamped(contract_service.sow_bytes(session, opp.id, version), session, version, stamp))]
    if kind == "MSA":
        data = contract_service.msa_bytes(session, opp.id, version, contract_service.MSA_MARK)
        return [(root + ["contracts"], f"MSA-{code}-R1-v{label}.docx", _stamped(data, session, version, stamp))] if data else []
    return []


def _mark_superseded(session: Session, store_, opp: models.Opportunity, version: models.ArtifactVersion) -> None:
    """Earlier approved baselines stay in BRD/ beside the new one, restamped SUPERSEDED, so only the latest reads APPROVED."""
    for earlier in _versions(session, opp.id, "BRD"):
        if earlier.id != version.id and earlier.status == "Superseded" and earlier.version_minor == 0 and earlier.version_major >= 1:
            store_.put([folder_name(opp), "BRD"], brd_docx.file_name(opp.code, earlier.version_label), brd_docx.render(session, opp.id, earlier))


@tracing.traced("drive_publish", lambda session, actor, opp_id, version: {"opportunity_id": opp_id, "role": actor.role, "artifact": version.artifact_type, "artifact_version": version.version_label}, lambda r: {"status": r}, "chain")
def publish(session: Session, actor, opp_id: str, version: models.ArtifactVersion) -> str:
    """Never raises for a store problem: returns published, pending or auth_expired."""
    opp = session.get(models.Opportunity, opp_id)
    try:
        store_ = storage.get_store()
        files = _files(session, opp, version)
        first = None
        for parts, name, data in files:
            placed = store_.put(parts, name, data)
            first = first or {**placed, "hash": opportunities.sha256(data)}
        if version.artifact_type == "BRD" and version.status == "Approved":
            _mark_superseded(session, store_, opp, version)
    except storage.AuthExpired:
        version.publish_status = AUTH_EXPIRED
    except storage.Unavailable:
        version.publish_status = PENDING
    else:
        version.publish_status = PUBLISHED
        if first:
            version.drive_file_id, version.drive_url, version.local_path, version.file_hash = first["file_id"], first["url"], first["local_path"], first["hash"]
    session.flush()
    store.audit(session, actor.name, actor.role, "artifact.published" if version.publish_status == PUBLISHED else "artifact.publish_pending", opportunity_id=opp_id, entity_type=version.artifact_type, version=version.version_label,
                details={"status": version.publish_status, "backend": storage.config()["storage"]["backend"]})
    return version.publish_status


@tracing.traced("drive_publish_input", lambda session, actor, opp_id, document: {"opportunity_id": opp_id, "role": actor.role}, lambda r: {"status": r}, "chain")
def publish_input(session: Session, actor, opp_id: str, document: models.Document) -> str:
    """The masked text only: the original file was never kept."""
    opp = session.get(models.Opportunity, opp_id)
    text = "\n\n".join(f"--- page {p['page']} ---\n{p['text']}" for p in document.masked_pages).encode()
    try:
        placed = storage.get_store().put([folder_name(opp), "inputs"], f"{document.filename}.masked.txt", text, TEXT_MIME)
    except storage.AuthExpired:
        document.publish_status = AUTH_EXPIRED
    except storage.Unavailable:
        document.publish_status = PENDING
    else:
        document.publish_status, document.drive_file_id, document.drive_url, document.local_path = PUBLISHED, placed["file_id"], placed["url"], placed["local_path"]
    session.flush()
    store.audit(session, actor.name, actor.role, "document.published" if document.publish_status == PUBLISHED else "document.publish_pending", opportunity_id=opp_id, entity_type="document", entity_id=str(document.id), details={"status": document.publish_status})
    return document.publish_status


def pending(session: Session, opp_id: str) -> dict:
    versions = session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.publish_status.in_((PENDING, AUTH_EXPIRED)))).all()
    documents = session.scalars(select(models.Document).where(models.Document.opportunity_id == opp_id, models.Document.publish_status.in_((PENDING, AUTH_EXPIRED)))).all()
    return {"versions": versions, "documents": documents, "auth_expired": any(x.publish_status == AUTH_EXPIRED for x in [*versions, *documents])}


def retry(session: Session, actor, opp_id: str) -> dict:
    waiting = pending(session, opp_id)
    results = [publish(session, actor, opp_id, v) for v in waiting["versions"]] + [publish_input(session, actor, opp_id, d) for d in waiting["documents"]]
    return {"tried": len(results), "published": results.count(PUBLISHED)}


def links(session: Session, opp_id: str) -> list[dict]:
    """Current documents and every version, for the opportunity screen."""
    rows = session.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id).order_by(models.ArtifactVersion.artifact_type, models.ArtifactVersion.version_major.desc(), models.ArtifactVersion.version_minor.desc())).all()
    return [{"artifact": v.artifact_type, "version": v.version_label, "status": v.status, "outdated": v.outdated, "publish": v.publish_status, "url": v.drive_url, "path": v.local_path, "hash": v.file_hash} for v in rows]
