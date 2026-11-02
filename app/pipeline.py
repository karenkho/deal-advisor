"""Opportunity pipeline: input files -> analyze_requirements (the v1 graph) -> opportunity record (versioned).

The UI processes only the bundled HRIS TOR; eval scripts call these functions with any files (TOR A/B, HRIS)."""
from __future__ import annotations

import hashlib
import time
import json
from dataclasses import dataclass

from app import brd, costing, decisions, determinism, estimation, llm, record, result_cache, risks, run_trace, tracing, spend
from app.config import SAMPLES_DIR, UPLOAD_PREFIX, estimation_config
from app.guards import mask_pii
from app.kvstore import KV
from app.statements import clean_reference, reference_order
from app.graph import PROMPT_VERSION, run
from app.suitability import assess_ai_suitability
from app.parsing import parse_bytes
from app.record import (Citation, Opportunity, OpportunityRecord, Question, RecordFeature, RecordRequirement,
                        SourceFile, Spend, UploadConfirmation)
from app.schemas import TorState

HRIS_FILES = [SAMPLES_DIR / "hris" / "tor.pdf"]
HRIS_BULLETIN = SAMPLES_DIR / "hris" / "bid_bulletin_01.pdf"
HRIS_BULLETIN_DATE = "2026-12-05"
HRIS_META = {"id": "hris", "client": "Kalinaw Trading Corporation (fictional)",
             "project_name": "Human Resource Information System (HRIS)"}


@dataclass
class DocInput:
    """A document in the opportunity. Its pages are already PII-masked: the raw file is released as soon as it is parsed,
    and nothing of an uploaded document is ever written to disk."""
    name: str
    kind: str
    pages: list[dict]
    date: str = ""
    number: str = ""
    pii_masked: int = 0
    bundled: bool = False

    @property
    def key(self) -> str:
        """Stable across runs, so a statement keeps its id when other documents change."""
        if self.kind == "tor":
            return "tor" if self.bundled or not self.name else "tor-" + hashlib.sha256(self.name.encode()).hexdigest()[:6]
        return f"{self.kind}-{self.number or self.date or self.name}"

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps([self.kind, self.date, self.number, [p["text"] for p in self.pages]]).encode()).hexdigest()


def make_doc(name: str, data: bytes, kind: str = "tor", date: str = "", number: str = "", bundled: bool = False) -> DocInput:
    """Parses and masks a file at once; the caller drops the bytes."""
    pages, pii = [], 0
    for pg in parse_bytes(data, name):
        text, n = mask_pii(pg["text"])
        pii += n
        pages.append({"page": pg["page"], "text": text})
    return DocInput(name=name, kind=kind, pages=pages, date=date, number=clean_reference(number), pii_masked=pii, bundled=bundled)


def _doc(p) -> DocInput:
    return p if isinstance(p, DocInput) else make_doc(p.name, p.read_bytes(), bundled=True)


def assemble(docs: list) -> tuple[list[DocInput], list[dict], list[dict]]:
    """Documents in a fixed order (TOR first, then by date), pages numbered continuously. Returns (documents, all pages, metadata)."""
    docs = sorted((_doc(p) for p in docs), key=lambda d: (d.kind != "tor", d.date, reference_order(d.number), d.fingerprint))
    pages, meta = [], []
    for d in docs:
        first = len(pages) + 1
        pages += [{"page": first + i, "text": pg["text"]} for i, pg in enumerate(d.pages)]
        meta.append({"key": d.key, "kind": d.kind, "number": d.number, "date": d.date, "name": d.name,
                     "first_page": first, "last_page": len(pages)})
    return docs, pages, meta


def upload_meta(docs: list[DocInput], client: str = "", project: str = "") -> dict:
    """The id follows the primary documents only, so adding a bulletin keeps the opportunity and its decisions."""
    primary = sorted(d.fingerprint for d in docs if d.kind == "tor")
    digest = hashlib.sha256("".join(primary).encode()).hexdigest()
    return {"id": f"{UPLOAD_PREFIX}{digest[:16]}", "client": client.strip() or "Client (uploaded TOR)",
            "project_name": project.strip() or "Uploaded TOR"}


def _fingerprints(docs: list[DocInput]) -> list[bytes]:
    return [d.fingerprint.encode() for d in docs]


@tracing.traced("analyze_requirements", lambda paths, use_cache=True, *a, **k: {"documents": len(paths), "use_cache": use_cache}, lambda r: {"features": len(r[0].features), "questions": len(r[0].questions), "from_cache": r[3]})
def analyze_requirements(paths: list, use_cache: bool = True, progress=None, kv: KV | None = None) -> tuple[TorState, dict, float, bool]:
    """Named agent tool: the graph (intake guard, statements, route, size, critic). Served from the full-result cache when
    available (zero Azure calls); otherwise only new or changed items call the LLM. Returns (state, usage totals, runtime_s,
    from_cache)."""
    docs, pages, meta = assemble(paths)
    files = _fingerprints(docs)
    memory = kv if kv is not None and kv.in_memory else None
    if use_cache:
        hit = result_cache.get(files, memory)
        if hit:
            return hit["state"], hit["usage"], hit["runtime_s"], True
    t0 = time.perf_counter()
    state, usage = run(pages, " + ".join(d.name for d in docs), progress=progress, docs=meta, kv=kv if use_cache else KV(enabled=False),
                       pii_masked=sum(d.pii_masked for d in docs))
    runtime = time.perf_counter() - t0
    totals = usage.totals()
    if use_cache:
        result_cache.put(files, state, totals, runtime, memory)
    return state, totals, runtime, False


def build_record(state: TorState, paths: list, meta: dict, usage: dict, runtime: float,
                 from_cache: bool) -> OpportunityRecord:
    reqs = {r.id: r for r in state.requirements}
    complexity = state.complexity.level if state.complexity else "standard"
    features = []
    for f in state.features:
        cites = [Citation(page=p, section=reqs[r].section, requirement_id=r)
                 for r in f.requirement_ids if r in reqs for p in reqs[r].pages] or [Citation(page=p) for p in f.pages]
        features.append(RecordFeature(
            id=f.id, epic=f.epic, name=f.name, extracted_name=f.extracted_name, description=f.description,
            type=f.type, priority=determinism.priority(f, reqs),
            complexity=determinism.functional_complexity(f, complexity),
            size=f.size, route=f.route, route_reason=f.route_reason, origin=f.origin,
            justification=f.justification or f.critic_reason, sources=f.sources, statement_ids=list(f.requirement_ids),
            superseded_ids=list(f.superseded_ids), assumptions=determinism.feature_assumptions(f),
            citations=cites, needs_manual_review=f.needs_manual_review,
            scope_status="suggested" if f.origin == "critic" else "in_scope"))
        features[-1].assumptions += [f"Coverage check also suggested {m}" for m in f.critic_merged]
    docs = [_doc(p) for p in paths]
    files = [SourceFile(name=d.name, sha256=d.fingerprint, pages=len(d.pages), kind=d.kind, number=d.number, date=d.date) for d in docs]
    kinds: dict[str, int] = {}
    for g in state.guard_findings:
        kinds[g.kind] = kinds.get(g.kind, 0) + 1
    effective_text = " ".join(r.text for r in state.requirements if not r.superseded_by)
    return OpportunityRecord(
        opportunity=Opportunity(**meta, files=files, domain_complexity=complexity,
                                complexity_reason=state.complexity.reason if state.complexity else "",
                                commercial_terms=determinism.commercial_terms(effective_text)),
        requirements=[RecordRequirement(section_role=r.section_role, id=r.id, text=r.text, category=r.category, section_no=r.section_no, doc_key=r.doc_key,
                                        doc_kind=r.doc_kind, doc_label=r.doc_label, doc_date=r.doc_date, file=r.file, area=r.area,
                                        superseded_by=r.superseded_by, home_section=r.home_section, citations=[Citation(page=p, section=r.section) for p in r.pages])
                      for r in state.requirements],
        features=features,
        questions=[Question(text=q.text, source=q.source, pages=q.pages, statement_ids=q.statement_ids) for q in state.questions]
        + [Question(text=q["text"], source="rule", pages=q["pages"], statement_ids=q.get("statement_ids", []))
           for q in determinism.rule_questions(state.features, state.requirements, [q.text for q in state.questions])],
        routed=state.routed, assumptions=state.assumptions, exclusions=state.exclusions, critic_notes=state.critic_notes,
        guardrails={"pages_read": state.pages_read, "pii_masked": state.pii_masked, "findings": kinds,
                    "warnings": state.warnings, "errors": state.errors, "closed_conflicts": state.closed_conflicts},
        spend=Spend(est_usd=round(usage.get("est_usd", 0.0), 6), prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)), from_cache=from_cache,
                    runtime_s=round(runtime, 1)))


def changes_between(old: OpportunityRecord, new: OpportunityRecord) -> dict:
    """The change list a re-process produces once the BRD is approved: by statement id and BR, nothing overwritten."""
    old_s, new_s = {r.id: r for r in old.requirements}, {r.id: r for r in new.requirements}
    old_f, new_f = {f.id: f for f in old.features}, {f.id: f for f in new.features}
    old_q, new_q = {q.text for q in old.questions}, {q.text for q in new.questions}
    return {"statements_added": [new_s[i].text for i in new_s if i not in old_s],
            "statements_removed": [old_s[i].text for i in old_s if i not in new_s],
            "newly_superseded": [f"{new_s[i].text} (by {new_s[i].superseded_by})" for i in new_s
                                 if new_s[i].superseded_by and i in old_s and not old_s[i].superseded_by],
            "brs_added": [new_f[i].name for i in new_f if i not in old_f],
            "brs_removed": [old_f[i].name for i in old_f if i not in new_f],
            "brs_changed": [new_f[i].name for i in new_f if i in old_f and set(new_f[i].statement_ids) != set(old_f[i].statement_ids)],
            "questions_opened": sorted(new_q - old_q), "questions_closed": sorted(old_q - new_q)}


def process(paths: list, meta: dict, rates: costing.RateTable | None = None, use_cache: bool = True,
            progress=None, kv: KV | None = None, current: OpportunityRecord | None = None,
            confirmation: dict | None = None) -> tuple[OpportunityRecord, TorState, dict]:
    """analyze_requirements -> record -> decisions -> assess_ai_suitability -> extract_brd -> estimation -> risks; saves a new
    record version at EXTRACTED: the BA reviews and approves the BRD next. Only new or changed items call the LLM. current: the
    record on screen; if its BRD is approved, the new result becomes a reviewable change list and overwrites nothing. Returns
    (record, state, internal costing view)."""
    rates = rates or costing.illustrative_rates()
    kv = kv or KV(enabled=use_cache)
    docs, _, _ = assemble(paths)
    with tracing.context(prompt_version=PROMPT_VERSION, model=llm.model_tag("MAIN"), light_model=llm.model_tag("LIGHT"), rates_source=rates.source):
        return _process(docs, paths, meta, rates, use_cache, progress, kv, current, confirmation)


def _process(docs: list, paths: list, meta: dict, rates: costing.RateTable, use_cache: bool, progress, kv: KV, current: OpportunityRecord | None, confirmation: dict | None) -> tuple[OpportunityRecord, TorState, dict]:
    state, usage, runtime, from_cache = analyze_requirements(docs, use_cache, progress, kv)
    rec = build_record(state, docs, meta, usage, runtime, from_cache)
    if not state.features:
        return rec, state, {}
    if confirmation:
        rec.upload_confirmation = UploadConfirmation(**confirmation)
    # decision memory: BA edits keyed by statement id come back on every re-process
    rec.features, rec.questions, rec.decisions_reapplied = decisions.apply(rec.features, rec.questions, decisions.load(meta["id"]))
    # the per-run cap covers the whole run: what the graph spent live counts against it
    spent = 0.0 if from_cache else usage.get("est_usd", 0.0)
    suit_usage = llm.Usage(limit_usd=max(spend.max_run_usd() - spent, 0.01))
    trace = run_trace.from_graph(state, from_cache)
    t0 = time.perf_counter()
    before = suit_usage.totals()
    try:
        rec.ai_suitability, suit_cached = assess_ai_suitability(rec.features, estimation_config(), suit_usage, use_cache=use_cache, kv=kv)
    finally:
        recorded = suit_usage.totals()["est_usd"]
        spend.record(recorded)
    trace = run_trace.add_step(trace, "assess_ai_suitability", int((time.perf_counter() - t0) * 1000), before,
                               suit_usage.totals(), f"{len(rec.ai_suitability)} activities classified")
    before = suit_usage.totals()
    if progress:
        progress("assess_ai_suitability", int((time.perf_counter() - t0) * 1000))
    t0 = time.perf_counter()
    try:
        rec.brd, brd_cached = brd.extract_brd(rec, suit_usage, kv)
    finally:
        spend.record(suit_usage.totals()["est_usd"] - recorded)
        recorded = suit_usage.totals()["est_usd"]
    trace = run_trace.add_step(trace, "extract_brd", int((time.perf_counter() - t0) * 1000), before, suit_usage.totals(),
                               f"{len(rec.brd.requirements)} BRs, {len(rec.brd.rules)} rules, {brd.metrics(rec.brd)['unsupported_items']} unsupported")
    before = suit_usage.totals()
    if progress:
        progress("extract_brd", int((time.perf_counter() - t0) * 1000))
    rec, view = estimation.run_estimation(rec, rates)
    t0 = time.perf_counter()
    try:
        calls: list[dict] = []
        rec = risks.evaluate_deal_risks(rec, rates.source, suit_usage, calls)
    finally:
        spend.record(suit_usage.totals()["est_usd"] - recorded)
    trace = run_trace.add_step(trace, "evaluate_deal_risks", int((time.perf_counter() - t0) * 1000), before,
                               suit_usage.totals(), f"{len(rec.risks)} risks, verdict {rec.recommendation.get('verdict')}", calls)
    rec.run_trace = trace
    if progress:
        progress("evaluate_deal_risks", int((time.perf_counter() - t0) * 1000))
    extra = suit_usage.totals()
    rec.spend.est_usd = round(rec.spend.est_usd + extra["est_usd"], 6)
    rec.spend.prompt_tokens += int(extra["prompt_tokens"])
    rec.spend.completion_tokens += int(extra["completion_tokens"])
    note = "status: processed TOR" + (" (from result cache: 0 Azure calls)" if from_cache and suit_cached and brd_cached else "")
    saved = record.save(rec, note)
    hits, misses = run_trace.cache_counts(trace)
    record.add_run(saved.opportunity.id, {"version": saved.version, "at": saved.created_at, "hits": hits, "misses": misses,
                                          "live_usd": round(extra["est_usd"] + spent, 6)})
    if current is not None and current.brd_approval.approved:   # approved = locked: a change list, not an overwrite
        kept = current.model_copy(deep=True)
        kept.pending_change = {"version": saved.version, "changes": changes_between(current, saved)}
        return record.save(kept, "status: re-process produced a change list"), state, view
    return saved, state, view


def process_upload(docs: list[DocInput], meta: dict, rates: costing.RateTable | None = None, progress=None,
                   current: OpportunityRecord | None = None, confirmation: dict | None = None):
    """Uploaded documents: everything stays in memory; nothing is written to disk."""
    return process(docs, meta, rates=rates, progress=progress, kv=KV(in_memory=True), current=current, confirmation=confirmation)


def hris_bulletin() -> DocInput:
    return make_doc(HRIS_BULLETIN.name, HRIS_BULLETIN.read_bytes(), kind="bid_bulletin", date=HRIS_BULLETIN_DATE, number="01", bundled=True)


def hris_docs(with_bulletin: bool = False) -> list[DocInput]:
    docs = [make_doc(p.name, p.read_bytes(), bundled=True) for p in HRIS_FILES]
    return docs + [hris_bulletin()] if with_bulletin else docs


def process_hris(rates: costing.RateTable | None = None, progress=None, with_bulletin: bool = False,
                 current: OpportunityRecord | None = None):
    return process(hris_docs(with_bulletin), HRIS_META, rates=rates, progress=progress, current=current)
