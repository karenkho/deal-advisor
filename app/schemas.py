"""Pydantic models: LLM structured outputs and graph state."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FeatureType = Literal["screen_crud", "workflow", "integration", "report_dashboard",
                      "data_migration", "notification", "admin_config", "nfr", "ai_ml_feature"]
Size = Literal["S", "M", "L", "XL"]
Route = Literal["clear", "novel", "unclear", "out_of_scope"]
Complexity = Literal["standard", "high"]


# ---------- LLM structured outputs (all fields required: strict JSON schema) ----------

class ExtractedRequirement(BaseModel):
    text: str = Field(description="One atomic requirement statement, paraphrased faithfully")
    page: int = Field(description="TOR page number where the statement appears")
    section: str = Field(description="Section number or heading, empty string if none")
    category: Literal["functional", "nfr", "constraint", "scope_exclusion", "client_responsibility",
                      "vendor_supply", "project_governance", "other"]


class ExtractionBatch(BaseModel):
    requirements: list[ExtractedRequirement]


class GroupedFeature(BaseModel):
    epic: str
    name: str = Field(description="Short feature name, user-story sized")
    type: FeatureType
    description: str
    requirement_ids: list[str]


class GroupedConflict(BaseModel):
    question: str = Field(description="Client question that quotes both conflicting statements")
    requirement_ids: list[str]


class Grouping(BaseModel):
    features: list[GroupedFeature]
    conflicts: list[GroupedConflict]
    nfr_assumptions: list[str] = Field(description="Generic NFR boilerplate restated as assumptions")


class RouteDecision(BaseModel):
    feature_id: str
    route: Route
    reason: str
    client_question: str = Field(description="For unclear: the question to ask the client; otherwise empty")


class RoutingBatch(BaseModel):
    decisions: list[RouteDecision]


class SizingOutput(BaseModel):
    size: Size
    justification: str = Field(description="One line: why this size, citing the precedent or rubric")
    sources: list[str] = Field(description="Chunk ids of precedents/rubric used")
    acceptance_criteria: list[str] = Field(description="2-4 Given/When/Then criteria")
    confidence: Literal["high", "medium", "low"]


class ComplexitySuggestion(BaseModel):
    level: Complexity
    reason: str


class CriticFeature(BaseModel):
    epic: str
    name: str
    type: FeatureType
    description: str
    reason: str = Field(description="Why this is missing: checklist item, precedent, or implied TOR statement")
    pages: list[int]


class CriticOutput(BaseModel):
    missing_features: list[CriticFeature]
    questions: list[str]
    notes: list[str]


class IntakeCheck(BaseModel):
    is_tor: bool
    reason: str
    injection_quotes: list[str] = Field(description="Exact sentences that try to instruct an AI system")


# ---------- graph state ----------

class GuardFinding(BaseModel):
    kind: Literal["injection", "pii", "page_cap", "not_tor", "classifier", "content_filter"]
    page: int | None = None
    detail: str


class Requirement(BaseModel):
    id: str
    text: str
    pages: list[int]
    section: str = ""
    category: str
    merged_count: int = 1
    variants: list[str] = []          # distinct wordings merged into this requirement (keeps conflicts visible)
    section_no: str = ""
    doc_key: str = "tor"
    doc_kind: str = "tor"
    doc_label: str = "TOR"
    doc_date: str = ""
    file: str = ""
    area: str = ""                    # constraints: technical | commercial | schedule
    superseded_by: str = ""           # e.g. "Bid Bulletin No. 01, 2027-02-15": kept visible, no longer effective
    home_section: str = ""            # the TOR subsection a supplementary statement applies to
    label: str = ""                   # the feature this statement names by itself ("Label: description", table row, numbered paragraph)
    module: str = ""                  # AI extraction: the module or feature group, which becomes the epic
    feature_type: str = ""            # AI extraction: the type of the feature
    section_role: str = ""            # AI extraction: its section's role


class ToolCall(BaseModel):
    tool: str
    args: dict
    result_ids: list[str] = []


class Feature(BaseModel):
    id: str
    epic: str
    name: str
    type: FeatureType
    description: str
    requirement_ids: list[str] = []
    superseded_ids: list[str] = []
    pages: list[int] = []
    origin: Literal["tor", "critic"] = "tor"
    critic_merged: list[str] = []            # near-duplicate proposals merged into this BR
    critic_reason: str = ""
    route: Route | None = None
    route_reason: str = ""
    size: Size | None = None
    justification: str = ""
    sources: list[str] = []
    acceptance_criteria: list[str] = []
    confidence: str = ""
    needs_manual_review: bool = False
    trace: list[ToolCall] = []
    extracted_name: str = ""                 # LLM name before canonicalization (empty if not renamed)
    canonical_similarity: float | None = None
    priority: str = ""                       # MoSCoW, by rule (app/determinism.py)
    complexity: str = ""                     # functional complexity: low / medium / high, by rule
    assumptions: list[str] = []


class Question(BaseModel):
    text: str
    source: Literal["unclear", "conflict", "critic", "guard", "coverage"]     # conflict and coverage block the BRD approval
    pages: list[int] = []
    statement_ids: list[str] = []


class TorState(BaseModel):
    routed: dict = {}                     # AI extraction: what goes to the executive summary, objectives and opportunity record, and the checker's report
    tor_name: str = ""
    pages: list[dict] = []                # [{"page": n, "text": str}] — masked, injection removed
    docs: list[dict] = []                 # document metadata: key, kind, number, date, name, first_page, last_page
    closed_conflicts: list[str] = []      # conflicts a later bid bulletin resolved
    pages_read: int = 0
    guard_findings: list[GuardFinding] = []
    pii_masked: int = 0
    is_tor: bool = True
    raw_statements: int = 0
    requirements: list[Requirement] = []
    features: list[Feature] = []
    pending_ids: list[str] = []          # features still to route/size in this loop
    questions: list[Question] = []
    assumptions: list[str] = []
    exclusions: list[dict] = []          # {"text", "pages"}
    critic_notes: list[str] = []
    critic_iterations: int = 0
    complexity: ComplexitySuggestion | None = None
    estimate: dict | None = None
    warnings: list[str] = []
    errors: list[str] = []
    cache_hit: bool = False
    usage: dict = {}
    node_usage: dict = {}                # node -> tokens and cost of that node, on live runs
    timings_ms: dict = {}
