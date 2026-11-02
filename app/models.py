"""The Deal Advisor schema (POC v3). The database is authoritative; documents are drafts generated from it.
Requirement and register IDs are identifiers, not positions: display order is sort_order, and rows are retired, never
reused. Version snapshots live in artifact_versions; these tables hold the current state."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (BigInteger, Boolean, Date, DateTime, Float, ForeignKey, ForeignKeyConstraint, Identity, Index, Integer,
                        MetaData, Text, UniqueConstraint, func)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={"ix": "ix_%(table_name)s_%(column_0_name)s", "uq": "uq_%(table_name)s_%(column_0_N_name)s",
                                           "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
                                           "pk": "pk_%(table_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s"})


def _id() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


def _opportunity() -> Mapped[str]:
    return mapped_column(ForeignKey("opportunities.id"), nullable=False, index=True)


def _json(default: str = "{}"):
    return mapped_column(JSONB, nullable=False, server_default=sql_text(f"'{default}'::jsonb"))


def _now() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Versioned:
    """Optimistic locking: a save against an old row_version is refused (see store.StaleWrite)."""
    @declared_attr
    def row_version(cls) -> Mapped[int]:
        return mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(),
                                                 onupdate=func.now())

    @declared_attr.directive
    def __mapper_args__(cls) -> dict:
        return {"version_id_col": cls.row_version}


# ---------- opportunities and inputs ----------

class Opportunity(Versioned, Base):
    __tablename__ = "opportunities"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    client_name: Mapped[str] = mapped_column(Text, nullable=False)
    bdm_name: Mapped[str | None] = mapped_column(Text)
    submission_deadline: Mapped[date | None] = mapped_column(Date)
    clarification_deadline: Mapped[date | None] = mapped_column(Date)
    commercial: Mapped[dict] = _json()      # requested duration, payment terms, warranty, budget note
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="Draft Opportunity")
    pipeline: Mapped[dict] = _json()        # AI suitability and domain complexity the calculators read
    award: Mapped[dict] = _json()           # outcome, date, note: Pending decision keeps status Proposal Submitted


class Document(Versioned, Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("opportunity_id", "sha256"), UniqueConstraint("opportunity_id", "doc_key"))
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    doc_key: Mapped[str] = mapped_column(Text, nullable=False)      # part of the statement id hash
    doc_type: Mapped[str] = mapped_column(Text, nullable=False)     # TOR | RFP | Bid bulletin | Minutes | Clarification | Other
    reference_no: Mapped[str | None] = mapped_column(Text)
    doc_date: Mapped[str | None] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    masked_pages: Mapped[list] = _json("[]")                        # [{"page", "text"}], PII already masked
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="received")
    uploaded_by: Mapped[str | None] = mapped_column(Text)
    uploaded_role: Mapped[str | None] = mapped_column(Text)
    publish_status: Mapped[str | None] = mapped_column(Text)
    drive_file_id: Mapped[str | None] = mapped_column(Text)
    drive_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)


class Statement(Base):
    __tablename__ = "statements"
    __table_args__ = (UniqueConstraint("opportunity_id", "statement_id"),)
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    statement_id: Mapped[str] = mapped_column(Text, nullable=False)     # ST-<hash>, from app.statements
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False)
    section: Mapped[str | None] = mapped_column(Text)
    section_no: Mapped[str | None] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    section_role: Mapped[str | None] = mapped_column(Text)              # AI extraction: requirements_scope, background, nfr, bid_procedure, ...
    classification: Mapped[str] = mapped_column(Text, nullable=False)
    area: Mapped[str | None] = mapped_column(Text)
    home_section: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list] = _json("[]")


# ---------- BRD ----------

class Epic(Versioned, Base):
    __tablename__ = "epics"
    __table_args__ = (UniqueConstraint("opportunity_id", "epic_id"),)
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    epic_id: Mapped[str] = mapped_column(Text, nullable=False)          # EP-01: stable
    name: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)


class Br(Versioned, Base):
    __tablename__ = "brs"
    __table_args__ = (UniqueConstraint("opportunity_id", "br_id"),
                      ForeignKeyConstraint(["opportunity_id", "epic_id"], ["epics.opportunity_id", "epics.epic_id"]))
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    br_id: Mapped[str] = mapped_column(Text, nullable=False)            # BR-014: stable, never reused
    epic_id: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(Text, nullable=False)
    complexity: Mapped[str | None] = mapped_column(Text)
    size: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    source_citations: Mapped[list] = _json("[]")
    statement_ids: Mapped[list] = _json("[]")
    scope_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="in_scope")  # suggested: not priced yet
    lifecycle_status: Mapped[str] = mapped_column(Text, nullable=False, server_default="Active")    # Active | Removed | Split | Merged
    split_from: Mapped[str | None] = mapped_column(Text)
    merged_into: Mapped[str | None] = mapped_column(Text)
    changed_by_document: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    legacy_id: Mapped[str | None] = mapped_column(Text)                 # the v2 id, e.g. BR-DS-01
    ext: Mapped[dict] = _json()                                         # v2 feature and BR detail the pipeline still reads


class EditDraft(Versioned, Base):
    """Auto-saved pending in-app edits per user: not a version."""
    __tablename__ = "edit_drafts"
    __table_args__ = (UniqueConstraint("opportunity_id", "user_name", "artifact_type"),)
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    user_name: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)
    pending: Mapped[dict] = _json()


class Register(Versioned):
    """A row of a BRD register, linked to BRs by br_ids."""
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    br_ids: Mapped[list] = _json("[]")
    statement_id: Mapped[str | None] = mapped_column(Text)
    citations: Mapped[list] = _json("[]")
    superseded_by: Mapped[str | None] = mapped_column(Text)
    unsupported: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    legacy_id: Mapped[str | None] = mapped_column(Text)
    ext: Mapped[dict] = _json()


def _register_unique() -> tuple:
    return (UniqueConstraint("opportunity_id", "item_id"),)


class BusinessRule(Register, Base):
    __tablename__ = "business_rules"
    __table_args__ = _register_unique()
    owner: Mapped[str | None] = mapped_column(Text)
    policy_reference: Mapped[str | None] = mapped_column(Text)


class Entity(Register, Base):
    __tablename__ = "entities"
    __table_args__ = _register_unique()


class Nfr(Register, Base):
    __tablename__ = "nfrs"
    __table_args__ = _register_unique()
    category: Mapped[str | None] = mapped_column(Text)
    metric_target: Mapped[str | None] = mapped_column(Text)
    measurable: Mapped[bool | None] = mapped_column(Boolean)


class ConstraintAssumption(Register, Base):
    __tablename__ = "constraints_assumptions"
    __table_args__ = _register_unique()
    item_type: Mapped[str] = mapped_column(Text, nullable=False)        # Constraint | Assumption | Dependency
    area: Mapped[str | None] = mapped_column(Text)
    impact: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="Open")


class OpenQuestion(Register, Base):
    __tablename__ = "open_questions"
    __table_args__ = _register_unique()
    source: Mapped[str | None] = mapped_column(Text)
    blocking: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="Open")    # Open | Answered | Acknowledged | Withdrawn
    answer_source: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str] = mapped_column(Text, nullable=False, server_default="client")    # client | internal: only client questions reach a client document
    withdrawn: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))    # {by, role, at, reason, previous_status}: kept, and out of every later version


class BrdSectionItem(Versioned, Base):
    """The other Appendix A rows with stable IDs: objectives (BO-), scope (IS-, OOS-), process overview, data and
    reporting, risks (R-), stakeholders."""
    __tablename__ = "brd_section_items"
    __table_args__ = (UniqueConstraint("opportunity_id", "section", "item_id"),)
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    section: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = _json()
    withdrawn: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))    # {by, role, at, reason}: kept, and out of every later version


class BrReview(Versioned, Base):
    __tablename__ = "br_reviews"
    __table_args__ = (UniqueConstraint("opportunity_id", "br_id"),
                      ForeignKeyConstraint(["opportunity_id", "br_id"], ["brs.opportunity_id", "brs.br_id"]))
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    br_id: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="Pending")  # Pending | Accepted | Needs revision | Deferred
    reviewer: Mapped[str | None] = mapped_column(Text)
    reviewer_role: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str | None] = mapped_column(Text)


class Decision(Base):
    """Decision memory (renames, merges, overrides, accepted scope), keyed by statement id, reapplied on re-processing."""
    __tablename__ = "decisions"
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    statement_ids: Mapped[list] = _json("[]")
    value: Mapped[object | None] = mapped_column(JSONB(none_as_null=True))
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_role: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = _now()


class IdCounter(Base):
    """The last number used per prefix (BR, EP, ...), so a retired ID is never handed out again."""
    __tablename__ = "id_counters"
    opportunity_id: Mapped[str] = mapped_column(ForeignKey("opportunities.id"), primary_key=True)
    prefix: Mapped[str] = mapped_column(Text, primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)


# ---------- artifacts and versions ----------

class ArtifactVersion(Versioned, Base):
    """QMS versioning: minor = controlled update or draft, major = approved baseline."""
    __tablename__ = "artifact_versions"
    __table_args__ = (UniqueConstraint("opportunity_id", "artifact_type", "version_major", "version_minor", name="uq_artifact_versions_version"),)
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)    # BRD | ESTIMATE | PROPOSAL | SOW | MSA
    version_major: Mapped[int] = mapped_column(Integer, nullable=False)
    version_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="Draft")   # Draft | Under validation | Approved | Superseded
    outdated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    based_on_id: Mapped[int | None] = mapped_column(ForeignKey("artifact_versions.id"))     # e.g. the BRD version an estimate uses
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=sql_text("'{}'::jsonb"), deferred=True)     # large: loaded when read
    file_hash: Mapped[str | None] = mapped_column(Text)
    drive_file_id: Mapped[str | None] = mapped_column(Text)
    drive_url: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    publish_status: Mapped[str | None] = mapped_column(Text)            # published | pending
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_role: Mapped[str] = mapped_column(Text, nullable=False)
    change_summary: Mapped[str | None] = mapped_column(Text)

    @property
    def version_label(self) -> str:
        return f"{self.version_major}.{self.version_minor}"


class ChangeRequest(Versioned, Base):
    __tablename__ = "change_requests"
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    artifact_type: Mapped[str] = mapped_column(Text, nullable=False)
    from_version_id: Mapped[int | None] = mapped_column(ForeignKey("artifact_versions.id"))
    trigger: Mapped[str] = mapped_column(Text, nullable=False)          # bulletin | re-upload | in-app edit
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"))
    diff_summary: Mapped[dict] = _json()
    estimate_impact: Mapped[dict] = _json()
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="Open")    # Open | Confirmed | Rejected
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_role: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_version_id: Mapped[int] = mapped_column(ForeignKey("artifact_versions.id"), nullable=False)
    approver_name: Mapped[str] = mapped_column(Text, nullable=False)
    approver_role: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)         # Approved | Rejected | Returned
    comment: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime] = _now()


# ---------- audit and runs ----------

class AuditLog(Base):
    """Append-only: the runtime role has INSERT and SELECT only. Created by infra/db_setup.sql, extended by migration.
    No foreign key to opportunities: the trail outlives whatever it describes."""
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = _now()
    actor_name: Mapped[str] = mapped_column(Text, nullable=False)
    actor_role: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict] = _json()
    opportunity_id: Mapped[str | None] = mapped_column(Text)


Index("ix_audit_log_opportunity_id", AuditLog.opportunity_id)


class Run(Versioned, Base):
    """Agent runs, replacing runs.json. Running at a container restart becomes Failed, with a retry."""
    __tablename__ = "runs"
    id: Mapped[int] = _id()
    opportunity_id: Mapped[str] = _opportunity()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="Running")     # Running | Succeeded | Failed
    started_at: Mapped[datetime] = _now()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, server_default=sql_text("0"))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    cache_hits: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    cache_misses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=sql_text("0"))
    detail: Mapped[dict] = _json()
