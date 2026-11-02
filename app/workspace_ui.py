"""The management workspace (POC v3): sign-in with a self-declared role, the opportunities list, and a workspace with one step per stage of the
workflow. Screens only: every rule lives in the services, and every click saves to the database."""
from __future__ import annotations

import hashlib
import json
import re

import pandas as pd
import streamlit as st
from sqlalchemy import func, select

from app import action_guard, brd_compare, brd_docx, brd_service as svc, contract_service as contracts, estimate_service as est, estimation, failures, llm, models, opportunities as ops, proposal_service as prop, purge, publishing, risks, spend, storage, store, supplement, units, workflow as wf
from app.config import SAMPLES_DIR, env
from app.db import session_scope
from app.logging_setup import log_event
from app.pipeline import hris_bulletin, make_doc
from app.workflow import Actor

MAX_UPLOAD_MB = 25
UPLOAD_WARNING = "POC: synthetic or public documents only. Do not upload confidential client documents."
UPLOAD_TYPES = ["pdf", "docx", "md", "txt"]
SUPPLEMENT_TYPES = ("Bid bulletin", "Minutes", "Clarification", "Other")
PROPOSAL_LABEL = {supplement.AMENDS: "Amends", supplement.ADDS: "Adds", supplement.REMOVES: "Removes", supplement.ANSWERS: "Answers a question",
                  supplement.NO_IMPACT: "No requirement impact", supplement.UNPLACED: "Could not be placed"}
TABLE_COLUMNS = {"title": "Title", "statement": "Requirement Statement", "priority": "Priority", "complexity": "Complexity", "scope_status": "Scope", "rationale": "Rationale"}
COMMERCIAL_LABELS = {"requested_months": "requested duration", "upfront_pct": "upfront payment", "final_pct": "final payment", "warranty_months": "warranty", "deliverables": "deliverables"}
CHANGED_STYLE = "background-color: rgba(255, 196, 0, 0.25)"


# ---------- identity ----------

def actor() -> Actor:
    return st.session_state.actor


def _audit_session(name: str, role: str, action: str, details: dict | None = None) -> None:
    with session_scope() as s:
        store.audit(s, name, role, action, entity_type="session", details=details)


@st.cache_resource
def _startup() -> int:
    """Once per process: a run a previous container left Running will never finish."""
    return ops.fail_orphaned_runs()


def require_identity() -> None:
    """After the password: a name and a role, self-declared and logged. Real sign-in is production scope."""
    if not env("DATABASE_URL"):
        st.error("DATABASE_URL is not configured. The app is locked.")
        st.stop()
    _startup()
    if "actor" in st.session_state:
        return
    st.title("AI Deal-to-Delivery Advisor")
    with st.form("identity"):
        name = st.text_input("Your name")
        role = st.selectbox("Your role", wf.ROLES)
        if st.form_submit_button("Continue"):
            if name.strip():
                st.session_state.actor = Actor(name.strip(), role)
                _audit_session(name.strip(), role, "session.signed_in")
                st.rerun()
            st.error("Enter your name.")
    st.stop()


def identity_sidebar() -> None:
    me = actor()
    st.caption(f"Signed in as **{me.name}** ({me.role}) — self-declared (POC)")
    role = st.selectbox("Switch role", wf.ROLES, index=wf.ROLES.index(me.role), key="switch_role", help="Logged, so one person can demo the whole flow.")
    if role != me.role:
        st.session_state.actor = Actor(me.name, role)
        _audit_session(me.name, role, "session.role_switched", {"from": me.role, "to": role})
        st.rerun()


# ---------- actions: a button that starts work is off while it runs, says so, and cannot run twice ----------

def _mark(key: str, label: str, token: str) -> None:
    """Runs before the script reruns, so the screen the click produces already shows the buttons off."""
    st.session_state._action_pending = {"key": key, "label": label, "token": token}


def _begin_render() -> None:
    """Valid for this one run of the script, so a click can never leave the buttons off."""
    st.session_state._action_now = st.session_state.pop("_action_pending", None)
    st.session_state._render_count = st.session_state.get("_render_count", 0) + 1
    st.session_state._render_token = f"screen-{st.session_state._render_count}"


def _clicked(key: str) -> bool:
    """Streamlit reports a disabled button as not clicked, so the click is read from the record, not from the button."""
    action = st.session_state.get("_action_now")
    return action is not None and action["key"] == key


def _button(target, label: str, **kwargs) -> bool:
    key = kwargs.get("key") or label
    kwargs["disabled"] = kwargs.get("disabled", False) or st.session_state.get("_action_now") is not None
    target.button(label, on_click=kwargs.pop("on_click", _mark), args=kwargs.pop("args", (key, label, st.session_state.get("_render_token", ""))), **kwargs)
    return _clicked(key)


def _submit(label: str, **kwargs) -> bool:
    kwargs["disabled"] = kwargs.get("disabled", False) or st.session_state.get("_action_now") is not None
    st.form_submit_button(label, on_click=_mark, args=(label, label, st.session_state.get("_render_token", "")), **kwargs)
    return _clicked(label)


def _once(action: dict | None, fn) -> None:
    """The server's guard. An opportunity's actions are checked in the database; before one exists, within this session."""
    if action is None or action.get("checked"):
        fn()
        return
    action["checked"] = True
    opp_id = st.session_state.get("opportunity")
    if opp_id:
        action_guard.guarded(opp_id, action["key"], actor().name, action["token"], fn)
        return
    done = st.session_state.setdefault("_actions_done", set())
    if (action["key"], action["token"]) in done:
        raise action_guard.AlreadyDone("Already done. Nothing was repeated.")
    done.add((action["key"], action["token"]))
    fn()


def _say(kind: str, message: str, action: dict | None) -> None:
    """After a click the buttons are off for this run, so the message moves to a fresh screen with them on again."""
    if action is None:
        getattr(st, kind)(message)
        return
    st.session_state._flash = (kind, message)
    st.rerun()


def _guarded(fn) -> bool:
    """Runs a change; a refused or stale action shows why and changes nothing. True when it went through."""
    action = st.session_state.get("_action_now")
    try:
        if action:
            with st.spinner(f"{action['label']}: working, please wait…"):
                _once(action, fn)
        else:
            fn()
        return True
    except (wf.NotAllowed, wf.InvalidTransition, ValueError, ops.DuplicateDocument, brd_docx.BrdFileError, svc.UnexpectedChange, prop.ProposalMismatch) as e:
        _say("error", str(e), action)
    except store.StaleWrite:
        _say("warning", store.STALE_MESSAGE, action)
    except llm.BudgetExceeded:
        _say("error", "The cost cap was reached, so this stopped and nothing was saved. An administrator can review the limit.", action)
    except Exception as e:  # noqa: BLE001 — the transaction rolled back; people see a plain message, the logs the type
        log_event("action failed", node="workspace", error=type(e).__name__)
        _say("error", f"That did not work and nothing was saved. Please try again; if it keeps happening, tell the team (error: {type(e).__name__}).", action)
    return False


def _act(fn) -> None:
    """One saved action, then a fresh screen."""
    done = {}

    def run():
        with session_scope() as s:
            done["ok"] = fn(s)

    if _guarded(run):
        st.rerun()


# ---------- opportunities ----------

def render(rates) -> None:
    _begin_render()
    flash = st.session_state.pop("_flash", None)
    if flash:
        getattr(st, flash[0])(flash[1])
    st.session_state.active_rates = rates
    current = st.session_state.get("opportunity")
    with session_scope() as s:
        exists = bool(current) and s.get(models.Opportunity, current) is not None
    if exists:
        _workspace(current)
    else:
        st.session_state.pop("opportunity", None)
        _list_screen()
    if st.session_state.get("_action_now") is not None:     # a click that did no work, such as a form with a field missing: the buttons come back on
        st.session_state._action_now = None
        st.rerun()


def _list_screen() -> None:
    st.subheader("Opportunities")
    with session_scope() as s:
        rows = ops.list_view(s)
    if rows:
        table = pd.DataFrame([{"Code": r["code"], "Client": r["client"], "Project": r["name"], "Status": r["status"], "BDM": r["bdm"], "Last updated": r["updated"].strftime("%Y-%m-%d %H:%M"),
                               "Next action": r["next_action"], "Next role": r["next_role"], "Outdated": r["outdated"]} for r in rows])
        st.dataframe(table, hide_index=True, width="stretch")
        names = {r["id"]: f"{r['code']} · {r['name']} · {r['status']}" for r in rows}
        pick, go = st.columns([4, 1], vertical_alignment="bottom")
        chosen = pick.selectbox("Open an opportunity", list(names), format_func=names.get, key="opportunity_pick")
        if go.button("Open", type="primary", key="opportunity_open", width="stretch"):
            st.session_state.opportunity = chosen
            st.rerun()
    else:
        st.info("No opportunities yet.")
    if wf.can(actor().role, wf.ACT_ADMIN):
        with st.expander("All audit entries (Admin)"):
            with session_scope() as s:
                entries = [{"When": a.occurred_at.strftime("%Y-%m-%d %H:%M:%S"), "Who": a.actor_name, "Role": a.actor_role, "Action": a.action, "Opportunity": a.opportunity_id or "", "Version": a.version or ""}
                           for a in s.scalars(select(models.AuditLog).order_by(models.AuditLog.id.desc()).limit(500))]
            st.dataframe(pd.DataFrame(entries), hide_index=True, width="stretch")
        _delete_opportunity(rows)
    if not wf.can(actor().role, wf.ACT_MANAGE_OPPORTUNITY):
        st.caption(f"Only a {wf.BDM} creates opportunities. Switch role in the sidebar to demo it.")
        return
    left, right = st.columns(2)
    with left:
        st.markdown("**Bundled sample**")
        st.caption("A fresh HRIS opportunity from the bundled sample, so demos never collide. Served from cached results: 0 Azure calls.")
        if _button(st, "Create sample HRIS opportunity", type="primary"):
            def create():
                with session_scope() as s:
                    opp_id = ops.create_sample_hris(s, actor())
                    run_id = ops.start_processing(s, actor(), opp_id)
                ops.submit(run_id, opp_id)
                st.session_state.opportunity = opp_id
            if _guarded(create):
                st.rerun()
    with right:
        _new_opportunity_form()


def _delete_opportunity(rows: list[dict]) -> None:
    """Admin only, and checked again in purge.purge."""
    with st.expander("Delete opportunity (Admin)"):
        st.warning("For test data. This permanently removes the opportunity's database rows, audit entries, runs, Drive folder, cached files and traces. It cannot be undone.")
        kept = sorted(purge.protected_codes())
        st.caption("Protected, cannot be deleted: " + ", ".join(kept) + ".")
        names = {r["id"]: f"{r['code']} · {r['name']} · {r['status']}" for r in rows if not purge.is_protected(r["code"])}
        if not names:
            st.info("Nothing can be deleted.")
            return
        with st.form("delete_opportunity", clear_on_submit=True):
            chosen = st.selectbox("Opportunity", list(names), format_func=names.get)
            typed = st.text_input("Type its code to confirm")
            if _submit("Delete opportunity"):
                done = {}

                def delete():
                    done["report"] = purge.purge(actor(), chosen, typed)
                if _guarded(delete):
                    _say("success", purge.report_text(done["report"]), st.session_state.get("_action_now"))


def _new_opportunity_form() -> None:
    with st.form("new_opportunity", clear_on_submit=True):
        st.markdown("**New opportunity**")
        c1, c2, c3 = st.columns(3)
        client = c1.text_input("Client name")
        project = c2.text_input("Project name")
        code = c3.text_input("Project code (e.g., HRIS)")
        d1, d2 = st.columns(2)
        submission = d1.date_input("Submission deadline", value=None)
        clarification = d2.date_input("Clarification deadline", value=None)
        m1, m2 = st.columns(2)
        months = m1.number_input("Requested duration (months)", 0, 120, 0)
        warranty = m2.number_input("Warranty (months)", 0, 120, 0)
        payment = st.text_input("Payment terms")
        notes = st.text_input("Notes")
        if _submit("Create opportunity"):
            commercial = {k: v for k, v in {"requested_months": months, "warranty_months": warranty, "payment_terms": payment, "notes": notes}.items() if v}

            def create():
                with session_scope() as s:
                    st.session_state.opportunity = ops.create(s, actor(), client, project, code, submission, clarification, commercial).id
            if _guarded(create):
                st.rerun()


def _banner(opp_id: str) -> dict:
    with session_scope() as s:
        state = ops.where_you_left_off(s, opp_id)
    last = f"Last: {state['last_action'].replace('.', ' ').replace('_', ' ')} by {state['last_by']}, {state['last_at']:%Y-%m-%d %H:%M}. " if state["last_action"] else ""
    role = f" — next: {state['next_role']}" if state["next_role"] else ""
    st.info(f"**Where you left off: {state['status']}.** {last}{state['pending']}{role}.")
    if state["outdated"]:
        st.warning("Outdated because the BRD changed: " + ", ".join(state["outdated"]) + ".")
    return state


STEPS = {"intake": "1 · Intake", "brd": "2 · BRD", "estimate": "3 · Estimate & Timeline", "proposal": "4 · Proposal", "award": "5 · Award", "contracts": "6 · Contracts", "final": "7 · Final Approval"}
STEP_OF_STATUS = {wf.DRAFT: "intake", wf.INPUTS: "intake", wf.BRD_GENERATED: "brd", wf.BRD_VALIDATION: "brd", wf.BRD_APPROVED: "estimate", wf.ESTIMATE_GENERATED: "estimate", wf.ESTIMATE_VALIDATION: "estimate",
                  wf.ESTIMATE_APPROVED: "proposal", wf.PROPOSAL_GENERATED: "proposal", wf.PROPOSAL_REVIEW: "proposal", wf.PROPOSAL_SUBMITTED: "award", wf.AWARDED: "contracts", wf.CONTRACTS_GENERATED: "contracts",
                  wf.LEGAL_REVIEW: "final", wf.CONTRACT_READY: "final", wf.CLOSED: "award"}


def _step_facts(opp_id: str, status: str) -> dict:
    """For each step: is there anything to see yet, its version, and whether it is outdated."""
    with session_scope() as s:
        latest = {a: ops.current_version(s, opp_id, a) for a in ("BRD", "ESTIMATE", "PROPOSAL", "SOW", "MSA")}
        award = dict(s.get(models.Opportunity, opp_id).award)
        facts = {a: (v.version_label, v.status, v.outdated) if v else None for a, v in latest.items()}
    order = wf.STATUSES.index(status)
    return {"intake": {"open": True, "version": None}, "brd": {"open": bool(facts["BRD"]), "version": facts["BRD"]},
            "estimate": {"open": bool(facts["ESTIMATE"]) or status == wf.BRD_APPROVED, "version": facts["ESTIMATE"]},
            "proposal": {"open": bool(facts["PROPOSAL"]) or status == wf.ESTIMATE_APPROVED, "version": facts["PROPOSAL"]},
            "award": {"open": bool(award) or status == wf.PROPOSAL_SUBMITTED, "version": None},
            "contracts": {"open": bool(facts["SOW"]) or status == wf.AWARDED, "version": facts["SOW"]},
            "final": {"open": wf.STATUSES.index(wf.CONTRACTS_GENERATED) <= order <= wf.STATUSES.index(wf.CONTRACT_READY), "version": facts["MSA"]}}


def _stepper(opp_id: str, state: dict) -> str:
    key, seen_key = f"step_{opp_id}", f"step_status_{opp_id}"
    if st.session_state.get(seen_key) != state["status"]:     # a new status moves you to where the work now is
        st.session_state[seen_key] = state["status"]
        st.session_state[key] = STEP_OF_STATUS[state["status"]]
    facts = _step_facts(opp_id, state["status"])
    active = st.session_state[key]
    for col, (step, label) in zip(st.columns(len(STEPS)), STEPS.items()):
        info = facts[step]
        if col.button(label, key=f"stepper_{step}", disabled=not info["open"], type="primary" if step == active else "secondary", width="stretch"):
            st.session_state[key] = step
            st.rerun()
        version = info["version"]
        col.caption((f"v{version[0]} · {version[1]}" + (" · Outdated" if version[2] else "")) if version else "—")
    return active


def _workspace(opp_id: str) -> None:
    if st.button("← All opportunities"):
        st.session_state.pop("opportunity")
        st.rerun()
    with session_scope() as s:
        opp = s.get(models.Opportunity, opp_id)
        title = f"{opp.code} · {opp.name} · {opp.client_name}"
    st.subheader(title)
    state = _banner(opp_id)
    with session_scope() as s:
        latest = s.scalar(select(func.max(models.AuditLog.id)).where(models.AuditLog.opportunity_id == opp_id))
    st.session_state._render_token = f"audit-{latest}"     # any saved action moves it, so a real repeat is allowed and a double-click is not
    _publishing_banner(opp_id)
    work, audit_tab, versions_tab, trace_tab, evidence_tab, record_tab = st.tabs(["Workspace", "Audit trail", "Versions", "Agent trace", "Evidence", "Record"], default="Agent trace" if st.session_state.get("trace_run") else None)
    with work:
        step = _stepper(opp_id, state)
        {"intake": _intake, "brd": _brd, "estimate": _estimate, "proposal": _proposal_step, "award": _award, "contracts": _contracts, "final": _final}[step](opp_id, state)
    with audit_tab:
        _audit(opp_id)
    with versions_tab:
        _all_versions(opp_id)
    with trace_tab:
        _agent_trace(opp_id)
    with evidence_tab:
        _evidence(opp_id)
    with record_tab:
        _record(opp_id)


def _publishing_banner(opp_id: str) -> None:
    with session_scope() as s:
        waiting = publishing.pending(s, opp_id)
        count, expired = len(waiting["versions"]) + len(waiting["documents"]), waiting["auth_expired"]
    if not count:
        return
    message = storage.AUTH_EXPIRED_MESSAGE if expired else storage.UNAVAILABLE_MESSAGE
    st.warning(f"{count} documents are waiting to be published (publish pending). {message} The workflow continues: the database is authoritative.")
    if _button(st, "Retry publish", key=f"retry_publish_{opp_id}"):
        _act(lambda s: publishing.retry(s, actor(), opp_id))


def _all_versions(opp_id: str) -> None:
    with session_scope() as s:
        rows = publishing.links(s, opp_id)
    if not rows:
        st.info("Nothing yet.")
        return
    for r in rows:
        link = f"[Open]({r['url']})" if r["url"] else (r["path"] or "")
        flag = " · Outdated" if r["outdated"] else ""
        st.markdown(f"**{r['artifact']} v{r['version']}** · {r['status']}{flag} · {r['publish'] or 'not published'} {link}")


def _agent_trace(opp_id: str) -> None:
    with session_scope() as s:
        pipeline = dict(s.get(models.Opportunity, opp_id).pipeline or {})
        rows = [r for r in s.scalars(select(models.Run).where(models.Run.opportunity_id == opp_id).order_by(models.Run.id.desc())) if not r.kind.startswith(action_guard.ACTION_PREFIX)]
        runs = [{"Reference": failures.reference(r.id), "When": r.started_at.strftime("%Y-%m-%d %H:%M"), "Step": r.kind, "Status": r.status, "Reason": r.detail.get("message", "") if r.status == "Failed" else "",
                 "Cost (USD)": round(r.cost_usd, 5), "Prompt tokens": r.prompt_tokens, "Completion tokens": r.completion_tokens} for r in rows]
        failed = [{"id": r.id, "detail": dict(r.detail), "started": r.started_at, "finished": r.finished_at, "cost": r.cost_usd, "tokens": r.prompt_tokens + r.completion_tokens} for r in rows if r.status == "Failed"]
    if runs:
        st.dataframe(pd.DataFrame(runs), hide_index=True, width="stretch")
    opened = st.session_state.get("trace_run")
    for run in failed:
        what, next_step = failures.text_of(run["id"], run["detail"])
        with st.expander(what, expanded=run["id"] == opened):
            st.markdown(f"**What to do next:** {next_step}")
            info = run["detail"]
            lines = [("Failed while", info.get("stage")), ("Technical type", info.get("error")), ("Database rule", info.get("constraint")), ("Started by", info.get("started_by")),
                     ("Started", f"{run['started']:%Y-%m-%d %H:%M:%S} UTC"), ("Finished", f"{run['finished']:%H:%M:%S} UTC" if run["finished"] else None), ("Cost (USD)", f"{run['cost']:.5f}"), ("Tokens", run["tokens"])]
            st.dataframe(pd.DataFrame([{"Item": k, "Value": str(v)} for k, v in lines if v not in (None, "")]), hide_index=True, width="stretch")
    if opened and st.button("Back to the workspace", key=f"trace_back_{opp_id}"):
        st.session_state.pop("trace_run")
        st.rerun()
    steps = (pipeline.get("run_trace") or {}).get("steps") or []
    if steps:
        st.markdown("**Generation steps**")
        st.dataframe(pd.DataFrame([{k: v for k, v in step.items() if isinstance(v, (str, int, float))} for step in steps]), hide_index=True, width="stretch")
    if not runs and not steps:
        st.info("No agent runs recorded for this opportunity.")


ROLE_LABELS = {"requirements_scope": "Requirements / scope", "background": "Background", "nfr": "Non-functional", "bid_procedure": "Bid procedure", "commercial_payment": "Commercial / payment",
               "timeline": "Timeline", "evaluation_criteria": "Evaluation criteria", "other": "Other"}


def _section_evidence(opp_id: str) -> None:
    """Why each section was or was not treated as scope: the AI's tag and reason, and where its rows and bullets went."""
    with session_scope() as s:
        pipeline = dict(s.get(models.Opportunity, opp_id).pipeline or {})
    sections = pipeline.get("sections") or []
    if not sections:
        st.caption("This opportunity's stored result has no section tags: it was read before they existed, or it is the bundled sample.")
        return
    st.markdown("**How each section was read**")
    st.caption("Every row and bullet in a section tagged Requirements / scope must map to a BR, an NFR or an explicit decision that it is not a requirement.")
    rows = [{"Section": f"{x['number']} {x['title']}".strip(), "Role": ROLE_LABELS.get(x["role"], x["role"]), "Why": x.get("reason", ""), "Rows and bullets": x.get("units", 0), "To BR": x.get("br", 0), "To NFR": x.get("nfr", 0),
             "Not a requirement": x.get("not_requirement", 0), "Unmapped": x.get("unmapped", 0)} for x in sections]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    unmapped = (pipeline.get("checker") or {}).get("unmapped") or []
    if unmapped:
        st.warning(f"{len(unmapped)} rows or bullets map to nothing: " + "; ".join(unmapped[:20]))
    rejected = pipeline.get("rejected") or []
    if rejected:
        st.warning(f"{len(rejected)} extracted items were rejected, because their quoted text is not in the document: " + "; ".join(r["quote"][:100] for r in rejected[:10]))


def _evidence(opp_id: str) -> None:
    _section_evidence(opp_id)
    path = SAMPLES_DIR / "evidence.json"
    if path.exists():
        st.caption("Measured on the bundled samples: stability and evaluation results.")
        st.json(json.loads(path.read_text()), expanded=1)
    else:
        st.info("No evidence file in this build.")


def _plain(text: str) -> str:
    """Document text shown as words: nothing in it may act as Markdown."""
    return re.sub(r"([\\`*_{}\[\]()#+!|~<>$:])", r"\\\1", " ".join(str(text).split()))


def _record(opp_id: str) -> None:
    """The authoritative record as stored. It never holds rate values."""
    with session_scope() as s:
        opp = s.get(models.Opportunity, opp_id)
        data = {"opportunity": {"id": opp.id, "code": opp.code, "name": opp.name, "client": opp.client_name, "bdm": opp.bdm_name, "status": opp.status, "commercial": opp.commercial, "award": opp.award},
                "versions": publishing.links(s, opp_id), "brd": store.brd_snapshot(s, opp_id)}
        bid_procedure = list((opp.commercial or {}).get("bid_procedure") or [])
    st.markdown("**Bid procedure** (from the document, kept on the record and never a requirement)")
    if bid_procedure:
        for item in bid_procedure:
            st.markdown(f"- {_plain(item)}")
    else:
        st.caption("None found in the documents.")
    st.json(json.loads(json.dumps(data, default=str)), expanded=1)


# ---------- intake ----------

def _intake(opp_id: str, state: dict) -> None:
    can_manage = wf.can(actor().role, wf.ACT_MANAGE_OPPORTUNITY)
    with session_scope() as s:
        opp = s.get(models.Opportunity, opp_id)
        commercial, row_version, status = dict(opp.commercial), opp.row_version, opp.status
        deadlines = (opp.submission_deadline, opp.clarification_deadline)
        client, name = opp.client_name, opp.name
        bundled = all(d.doc_key == "tor" for d in ops.documents(s, opp_id))
        docs = [{"Type": d.doc_type, "Reference": d.reference_no or "", "Date": d.doc_date or "", "File": d.filename, "Pages": len(d.masked_pages), "Status": d.status, "Uploaded by": f"{d.uploaded_by} ({d.uploaded_role})"} for d in ops.documents(s, opp_id)]
        run = ops.latest_run(s, opp_id)
        run_view = {"status": run.status, "id": run.id, "detail": dict(run.detail)} if run else None
    with st.form(f"intake_{row_version}"):
        st.markdown("**Client and commercial requirements**")
        if commercial.get("from_tor") and not commercial.get("confirmed"):
            st.warning("Filled from the TOR: " + ", ".join(COMMERCIAL_LABELS.get(k, k) for k in commercial["from_tor"]) + ". Confirm or edit.")
        c1, c2 = st.columns(2)
        new_client = c1.text_input("Client name", client, disabled=not can_manage)
        new_name = c2.text_input("Project name", name, disabled=not can_manage)
        d1, d2 = st.columns(2)
        submission = d1.date_input("Submission deadline", deadlines[0], disabled=not can_manage)
        clarification = d2.date_input("Clarification deadline", deadlines[1], disabled=not can_manage)
        m1, m2, m3, m4 = st.columns(4)
        months = m1.number_input("Requested duration (months)", 0, 120, int(commercial.get("requested_months") or 0), disabled=not can_manage)
        upfront = m2.number_input("Upfront (%)", 0, 100, int(commercial.get("upfront_pct") or 0), disabled=not can_manage)
        final = m3.number_input("Final (%)", 0, 100, int(commercial.get("final_pct") or 0), disabled=not can_manage)
        warranty = m4.number_input("Warranty (months)", 0, 120, int(commercial.get("warranty_months") or 0), disabled=not can_manage)
        payment = st.text_input("Payment terms", commercial.get("payment_terms", ""), disabled=not can_manage)
        budget = st.text_input("Budget note", commercial.get("budget_note", ""), disabled=not can_manage)
        notes = st.text_input("Notes", commercial.get("notes", ""), disabled=not can_manage)
        if _submit("Save and confirm", disabled=not can_manage):
            changes = {"client_name": new_client, "name": new_name, "submission_deadline": submission, "clarification_deadline": clarification,
                       "commercial": {**commercial, "requested_months": months, "upfront_pct": upfront, "final_pct": final, "warranty_months": warranty, "payment_terms": payment, "budget_note": budget, "notes": notes, "confirmed": True}}
            _act(lambda s: ops.update_intake(s, actor(), opp_id, row_version, **changes))

    st.markdown("**Documents**")
    if docs:
        st.dataframe(pd.DataFrame(docs), hide_index=True, width="stretch")
    if run_view and run_view["status"] == "Running":
        _run_progress(opp_id)
    elif run_view and run_view["status"] == "Failed" and status == wf.INPUTS:
        what, next_step = failures.text_of(run_view["id"], run_view["detail"])
        st.error(f"**{what}** {next_step}")
        if st.button("See the details in Agent trace", key=f"failure_details_{run_view['id']}"):
            st.session_state.trace_run = run_view["id"]
            st.rerun()
    if status == wf.INPUTS and can_manage and not (run_view and run_view["status"] == "Running"):
        label = "Retry" if run_view and run_view["status"] == "Failed" else "Generate draft BRD"
        pages = sum(d["Pages"] for d in docs)
        allowed, message = (True, "Cached: 0 Azure calls.") if bundled else spend.preflight(pages, full=True)[:2]
        st.caption(message)
        if _button(st, label, type="primary", disabled=not allowed):
            def start():
                with session_scope() as s:
                    run_id = ops.start_processing(s, actor(), opp_id)
                ops.submit(run_id, opp_id, confirmation=st.session_state.get("upload_confirmation"))
            if _guarded(start):
                st.rerun()
    if can_manage and status != wf.CLOSED:
        _upload(opp_id, status)


@st.fragment(run_every=2)
def _run_progress(opp_id: str) -> None:
    with session_scope() as s:
        run = ops.latest_run(s, opp_id)
        running = run is not None and run.status == "Running"
    if running:
        st.info("The draft BRD is being generated on the server. You can close this page; the result will be here when you return.")
    else:
        st.rerun()


def _upload(opp_id: str, status: str) -> None:
    has_primary = status != wf.DRAFT
    st.markdown("**Add a document**" if has_primary else "**Upload the TOR or RFP**")
    st.warning(UPLOAD_WARNING)
    generation = st.session_state.get("intake_upload_generation", 0)
    kinds = SUPPLEMENT_TYPES if has_primary else ("TOR", "RFP")
    c1, c2, c3 = st.columns(3)
    doc_type = c1.selectbox("Document type", kinds, key=f"up_type_{generation}")
    doc_date = c2.date_input("Document date", value=None, key=f"up_date_{generation}")
    number = c3.text_input("Reference no.", key=f"up_number_{generation}")
    upload = st.file_uploader("File (PDF, DOCX, MD, TXT)", type=UPLOAD_TYPES, key=f"up_file_{generation}")
    confirmed = st.checkbox("I confirm this is a synthetic or public document, not a confidential client document.", key=f"up_confirm_{generation}")
    needs_reference = doc_type == "Bid bulletin" and not number.strip()
    if needs_reference:
        st.caption("A bid bulletin needs a reference number.")
    if upload is not None and upload.size > MAX_UPLOAD_MB * 1024 * 1024:
        st.error(f"The file is over {MAX_UPLOAD_MB} MB.")
        upload = None
    if _button(st, "Add document", disabled=not (upload and confirmed) or needs_reference or (doc_type not in ("TOR", "RFP") and not doc_date)):
        kind = ops.PIPELINE_KIND[doc_type]
        try:
            parsed = make_doc(upload.name, upload.getvalue(), kind=kind, date=doc_date.isoformat() if doc_date else "", number=number.strip())
        except Exception as e:  # noqa: BLE001 — a damaged or unsupported file
            st.error(f"The file could not be read ({type(e).__name__}).")
            return
        if not parsed.pages:
            st.error("No readable text found. A scanned PDF needs text recognition first.")
            return
        st.session_state.upload_confirmation = {"confirmed_by": actor().name, "confirmed_at": store_now(), "files": 1}
        _add_document(opp_id, parsed, doc_type, generation)
    if status not in (wf.DRAFT, wf.INPUTS) and _button(st, "Add sample bid bulletin (HRIS)", key=f"ws_sample_bulletin_{generation}"):
        _add_document(opp_id, hris_bulletin(), "Bid bulletin", generation)


def store_now() -> str:
    from app.record import _now
    return _now()


def _add_document(opp_id: str, parsed, doc_type: str, generation: int) -> None:
    def add():
        with session_scope() as s:
            row = ops.add_document(s, actor(), opp_id, parsed, doc_type)
            if doc_type not in ops.PRIMARY_TYPES and s.get(models.Opportunity, opp_id).status not in (wf.DRAFT, wf.INPUTS):
                supplement.propose(s, actor(), opp_id, row.id)
    if _guarded(add):
        st.session_state.intake_upload_generation = generation + 1
        st.rerun()


# ---------- BRD ----------

def _brd(opp_id: str, state: dict) -> None:
    with session_scope() as s:
        has_brd = ops.current_version(s, opp_id, "BRD") is not None
    if not has_brd:
        st.info("The draft BRD appears here once the TOR is processed.")
        return
    specialist = wf.can(actor().role, wf.ACT_VALIDATE_BRD)
    open_for_edit = specialist and state["status"] in (wf.BRD_GENERATED, wf.BRD_VALIDATION)
    requirements_tab, versions_tab = st.tabs(["Requirements", "Versions"])
    with requirements_tab:
        _change_requests(opp_id, specialist)
        _validation_header(opp_id, state, specialist)
        _requirements_table(opp_id, open_for_edit)
        can_change = specialist and (open_for_edit or svc.is_past_approval(state["status"]))
        with st.expander("Re-upload an edited BRD", expanded=False):
            _reupload(opp_id, can_change, state)
        if can_change:
            with st.expander("Insert, split, merge or move requirements"):
                _structure(opp_id)
        _registers(opp_id, specialist)
    with versions_tab:
        _versions(opp_id)


def _validation_header(opp_id: str, state: dict, specialist: bool) -> None:
    done, total = state["reviewed"], state["total"]
    st.progress(done / total if total else 0.0, text=f"{done} of {total} reviewed · BRD v{state['brd_version']}")
    with session_scope() as s:
        blockers = svc.approval_blockers(s, opp_id)
    c1, c2, c3 = st.columns(3)
    if state["status"] == wf.BRD_GENERATED and _button(c1, "Start validation", disabled=not specialist, type="primary"):
        _act(lambda s: svc.start_validation(s, actor(), opp_id))
    if state["status"] == wf.BRD_VALIDATION and done < total and _button(c2, "Accept all remaining", disabled=not specialist):
        _act(lambda s: svc.accept_all_remaining(s, actor(), opp_id))
    if state["status"] == wf.BRD_VALIDATION and _button(c3, "Approve BRD", type="primary", disabled=not specialist or bool(blockers)):
        _act(lambda s: svc.approve(s, actor(), opp_id, expected_row_version=state["row_version"]))
    if state["status"] == wf.BRD_VALIDATION and blockers:
        st.caption("Approval needs: " + " ".join(blockers))
    if not specialist:
        st.caption(f"Only a {wf.SPECIALIST} validates the BRD. You can read it and download it.")


def _requirements_table(opp_id: str, editable: bool) -> None:
    request = st.session_state.pop(f"save_request_{opp_id}", None)
    if request:     # before the table exists, so the table comes back from the saved version
        _save_edits(opp_id, *request)
    with session_scope() as s:
        brs = svc.active_brs(s, opp_id)
        reviews = {r.br_id: r for r in s.scalars(select(models.BrReview).where(models.BrReview.opportunity_id == opp_id))}
        documents = {d.id: supplement._label(d) for d in ops.documents(s, opp_id)}
        rows = [{"BR ID": b.br_id, "Review": reviews[b.br_id].status, "Changed": ", ".join(TABLE_COLUMNS.get(k, k) for k in b.ext.get("ai_original", {}) if k in TABLE_COLUMNS), "Epic": b.epic_id, "Title": b.title, "Requirement Statement": b.statement, "Priority": b.priority, "Complexity": b.complexity or "", "Scope": b.scope_status, "Rationale": b.rationale or "", "Flag": brd_docx.NEEDS_REWORDING if b.ext.get("needs_rewording") else "",
                 "Comment": reviews[b.br_id].comment or "", "Changed by": documents.get(b.changed_by_document, ""),
                 "AI original": "; ".join(f"{TABLE_COLUMNS.get(k, k)}: {v}" for k, v in b.ext.get("ai_original", {}).items()), "_row": b.row_version, "_rev": reviews[b.br_id].row_version} for b in brs]
        saved_draft = ops.load_draft(s, actor(), opp_id, "BRD") if editable else {}
    changed_docs = sorted({r["Changed by"] for r in rows if r["Changed by"]})
    only = st.selectbox("Show", ["All requirements"] + [f"Changed by {d}" for d in changed_docs], key=f"brd_filter_{opp_id}")
    if only != "All requirements":
        rows = [r for r in rows if f"Changed by {r['Changed by']}" == only]
    seen_key = f"draft_seen_{opp_id}"
    undecided = bool(saved_draft) and not st.session_state.get(seen_key)     # kept untouched until the user chooses
    if undecided:
        st.warning(f"You have unsaved edits on {len(saved_draft)} requirements from an earlier session.")
        restore, discard = st.columns(2)
        if _button(restore, "Restore unsaved edits?", key=f"restore_{opp_id}"):
            st.session_state[f"draft_applied_{opp_id}"] = saved_draft
            st.session_state[seen_key] = True
            st.rerun()
        if _button(discard, "Discard them", key=f"discard_{opp_id}"):
            st.session_state[seen_key] = True
            _act(lambda s: ops.clear_draft(s, actor(), opp_id, "BRD"))
    else:
        st.session_state[seen_key] = True
    applied = st.session_state.get(f"draft_applied_{opp_id}", {})
    base = pd.DataFrame([{**r, **{TABLE_COLUMNS[k]: v for k, v in applied.get(r["BR ID"], {}).items() if k in TABLE_COLUMNS}} for r in rows])
    if base.empty:
        st.info("No requirements to show.")
        return
    visible = base.drop(columns=["_row", "_rev"])
    editable = editable and not undecided
    locked = ["BR ID", "Changed", "Epic", "Flag", "Changed by", "AI original"] if editable else list(visible.columns)
    edited = st.data_editor(_highlight(visible), hide_index=True, width="stretch", disabled=locked, key=f"brd_table_{opp_id}_{base['_row'].sum()}_{base['_rev'].sum()}",
                            column_config={"BR ID": st.column_config.TextColumn("BR ID (do not edit)"), "Priority": st.column_config.SelectboxColumn(options=list(svc.PRIORITIES), required=True),
                                           "Complexity": st.column_config.SelectboxColumn(options=list(svc.COMPLEXITIES)), "Scope": st.column_config.SelectboxColumn(options=list(svc.SCOPE_STATUSES), required=True), "Review": st.column_config.SelectboxColumn(options=list(svc.REVIEW_STATUSES), required=True)})
    if not editable:
        return
    was = {r["BR ID"]: r for r in rows}
    pending = {}
    for r in edited.to_dict("records"):
        fields = {field: r[label] for field, label in TABLE_COLUMNS.items() if str(r[label] or "") != str(was[r["BR ID"]][label] or "")}
        if fields:
            pending[r["BR ID"]] = fields
    review_click = next(((r["BR ID"], r["Review"], r["Comment"]) for r in edited.to_dict("records") if (r["Review"], r["Comment"] or "") != (was[r["BR ID"]]["Review"], was[r["BR ID"]]["Comment"])), None)
    if review_click:
        br_id, status, comment = review_click
        expected = was[br_id]["_rev"]
        st.session_state[f"draft_applied_{opp_id}"] = pending       # the table is rebuilt after a review: unsaved edits stay on it
        with session_scope() as s:
            ops.save_draft(s, actor(), opp_id, "BRD", pending)
        _act(lambda s: svc.set_review(s, actor(), opp_id, br_id, status, comment or None, expected))
        return
    with session_scope() as s:
        ops.save_draft(s, actor(), opp_id, "BRD", pending)
    label = f"Save changes ({sum(len(f) for f in pending.values())} unsaved)"
    _button(st, label, type="primary", disabled=not pending, key=f"save_{opp_id}", on_click=_request_save, args=(opp_id, pending, {b: was[b]["_row"] for b in pending}, label, st.session_state.get("_render_token", "")))
    if pending:
        st.caption("Unsaved edits are kept as a draft; each save creates a new minor version.")


def _highlight(frame: pd.DataFrame):
    """The Changed column, read-only so its colour shows: Streamlit does not style cells in editable columns. It lists what differs from the AI original."""
    return frame.style.apply(lambda col: [CHANGED_STYLE if value else "" for value in col], subset=["Changed"])


def _request_save(opp_id: str, pending: dict, versions: dict, label: str, token: str) -> None:
    _mark(f"save_{opp_id}", label, token)
    st.session_state[f"save_request_{opp_id}"] = (pending, versions)


def _save_edits(opp_id: str, pending: dict, versions: dict) -> None:
    def save():
        with session_scope() as s:
            current = svc.all_brs(s, opp_id)
            for br_id, seen in versions.items():
                if current[br_id].row_version != seen:
                    raise store.StaleWrite
            changes = svc.empty_changes()
            changes["modified"] = [{"br_id": b, "changes": {k: (v or None) if k in ("rationale", "complexity") else v for k, v in fields.items()}} for b, fields in pending.items()]
            svc.apply_changes(s, actor(), opp_id, changes, "in-app edit")
            ops.clear_draft(s, actor(), opp_id, "BRD")
    if _guarded(save):
        st.session_state.pop(f"draft_applied_{opp_id}", None)
        st.rerun()


def _reupload(opp_id: str, can_change: bool, state: dict) -> None:
    with session_scope() as s:
        data = brd_docx.render(s, opp_id)
        opp = s.get(models.Opportunity, opp_id)
        name = brd_docx.file_name(opp.code, ops.current_version(s, opp_id, "BRD").version_label)
    hits = brd_docx.leak_hits(data, st.session_state.get("active_rates"))
    if hits:
        st.error("Download blocked: internal data in " + ", ".join(sorted({h["section"] for h in hits})) + ".")
    else:
        st.download_button(f"Download {name}", data, file_name=name, key=f"download_current_{opp_id}")
    st.caption("Edit the Business Requirements table: leave a BR ID as it is, or leave it empty for a new row. To split or merge, write “split from BR-012” or “merged into BR-008” in Notes.")
    if not can_change:
        return
    upload = st.file_uploader("Edited BRD (DOCX)", type=["docx"], key=f"reupload_{opp_id}_{state['brd_version']}")
    if upload is None:
        return
    choices: dict = st.session_state.setdefault(f"parents_{opp_id}", {})
    try:
        with session_scope() as s:
            result = brd_docx.diff(s, opp_id, upload.getvalue(), choices)
    except brd_docx.BrdFileError as e:
        st.error(str(e))
        return
    with session_scope() as s:
        active = [b.br_id for b in svc.active_brs(s, opp_id)]
    for item in result["unlinked"]:
        parent = st.selectbox(f"Where did the new row “{item['title']}” come from?", ["(choose)", "A new requirement"] + active, key=f"parent_{item['index']}")
        if parent != "(choose)":
            choices[item["index"]] = None if parent == "A new requirement" else parent
            st.rerun()
    if result["unlinked"]:
        return
    changes = result["changes"]
    if not svc.has_changes(changes) and not result["text_diffs"]:
        st.info("This file has no changes.")
        return
    _show_diff(opp_id, result)
    if not svc.has_changes(changes):
        st.warning("Nothing here can be applied: these sections are not read back from a file. Change them in the app.")
    _estimate_impact(opp_id, changes, upload.getvalue())
    confirmed = True
    if changes["removed"]:
        confirmed = st.checkbox("Confirm the removal of " + ", ".join(changes["removed"]) + ". Their IDs stay retired and are never reused.", key=f"confirm_removed_{opp_id}")
    if changes["withdrawn"] or changes["reinstated"]:
        if changes["withdrawn"]:
            reason = st.text_input("Reason for withdrawing (optional)", key=f"withdraw_reason_{opp_id}")
            changes["withdrawn"] = [{**w, "reason": reason.strip() or None} for w in changes["withdrawn"]]
        ids = [w["item_id"] for w in changes["withdrawn"] + changes["reinstated"]]
        confirmed = st.checkbox("Confirm: " + ", ".join(ids) + ". Withdrawn items are kept with who, when and why, and leave this and later versions.", key=f"confirm_withdrawn_{opp_id}") and confirmed
    apply_col, reject_col = st.columns(2)
    if _button(apply_col, "Apply", type="primary", disabled=not confirmed or not svc.has_changes(changes), key=f"apply_{opp_id}"):
        def apply():
            with session_scope() as s:
                svc.apply_changes(s, actor(), opp_id, changes, "re-upload")
        if _guarded(apply):
            st.session_state.pop(f"parents_{opp_id}", None)
            st.rerun()
    if _button(reject_col, "Reject", key=f"reject_{opp_id}"):
        st.session_state.pop(f"parents_{opp_id}", None)
        st.info("Nothing was changed. Remove the file above to continue.")


def _estimate_impact(opp_id: str, changes: dict, data: bytes) -> None:
    """Man-days and price if this were applied. Calculators only: no LLM, nothing written. Kept per file, because it takes a second."""
    key = f"reupload_delta_{opp_id}_{hashlib.sha256(data).hexdigest()}"
    if key not in st.session_state:
        rates, discount = _rates()
        with session_scope() as s:
            try:
                st.session_state[key] = est.preview_delta(s, actor(), opp_id, changes, rates, discount)
            except (svc.UnexpectedChange, wf.InvalidTransition, ValueError) as e:
                st.warning(f"The estimate impact could not be calculated: {e}")
                return
    delta = st.session_state[key]
    if delta is None:
        st.caption("No estimate exists yet, so there is no man-day or price change to show.")
    else:
        st.markdown("**Impact on the estimate** (calculators only, no LLM: USD 0.00)")
        _delta_view(delta)


def _show_diff(opp_id: str, result: dict) -> None:
    changes = result["changes"]
    if result["renumbered"]:
        st.warning("The IDs in this file were renumbered. Each row was matched to its original ID by its text: " + ", ".join(f"{r['file_id']} in the file is {r['item_id']}" for r in result["renumbered"]) + ".")
    if changes["withdrawn"]:
        st.markdown("**Removed from the file** (withdrawn on apply, after you confirm)")
        st.table(pd.DataFrame([{"ID": w["item_id"], "Type": brd_docx.SECTION_LABEL[w["section"]], "Change": "removed", "Text": w["text"]} for w in changes["withdrawn"]]).set_index("ID"))
        if any(w["section"] == "in_scope" for w in changes["withdrawn"]):
            st.caption("Withdrawing a scope row does not remove the BRs under it: remove those in the Business Requirements table.")
    if changes["reinstated"]:
        st.markdown("**Withdrawn earlier, back in the file** (reinstated on apply, after you confirm)")
        st.table(pd.DataFrame([{"ID": r["item_id"], "Type": brd_docx.SECTION_LABEL[r["section"]], "Change": "reinstated", "Text": r["text"]} for r in changes["reinstated"]]).set_index("ID"))
    edited = [{"ID": q["item_id"], "Question": q.get("text", "(unchanged)"), "Status": q.get("status", "(unchanged)"), "Answer source": q.get("answer_source", "(unchanged)")} for q in changes["questions"]]
    edited += [{"ID": i["item_id"], "Question": "Scope, objective or risk row edited", "Status": "", "Answer source": ""} for i in changes["sections"] if i.get("item_id")]
    if edited:
        st.markdown("**Edited questions and items**")
        st.table(pd.DataFrame(edited).set_index("ID"))
    with session_scope() as s:
        brs = svc.all_brs(s, opp_id)
        rows = [{"BR ID": m["br_id"], "Field": TABLE_COLUMNS.get(f, "Epic" if f == "epic_id" else f), "Current": getattr(brs[m["br_id"]], f) or "", "In the file": v or ""} for m in result["changes"]["modified"] for f, v in m["changes"].items()]
    if rows:
        st.markdown("**Changes, side by side**")
        st.table(pd.DataFrame(rows).set_index("BR ID"))     # wraps: a long statement must be readable in full to compare it
    if result["changes"]["new"]:
        st.markdown("**New requirements** (get the next unused numbers)")
        st.dataframe(pd.DataFrame([{"Epic": n["epic_id"], "Title": n["title"], "Statement": n["statement"], "Split from": n["split_from"] or ""} for n in result["changes"]["new"]]), hide_index=True, width="stretch")
    if result["impact"]:
        st.markdown("**Impact on size** (by rule, no LLM: USD 0.00)")
        st.dataframe(pd.DataFrame([{"BR": i.get("title") or i["br_id"], "Change": i["change"], "Size": i["size_from"] or "—", "New size": i["size_to"] or "—"} for i in result["impact"]]), hide_index=True, width="stretch")
    for d in result["text_diffs"]:
        with st.expander(f"Text changes in {d['section']} (shown only)"):
            st.code("\n".join(d["diff"]))


def _structure(opp_id: str) -> None:
    st.caption("BR numbers are identifiers. They are never renumbered or reused; display order is separate.")
    with session_scope() as s:
        brs = {b.br_id: b.title for b in svc.active_brs(s, opp_id)}
        epics = {e.epic_id: e.name for e in s.scalars(select(models.Epic).where(models.Epic.opportunity_id == opp_id).order_by(models.Epic.sort_order))}
    label = lambda b: f"{b} · {brs[b]}"  # noqa: E731
    epic_label = lambda e: f"{e} · {epics[e]}"  # noqa: E731
    insert, split, merge, move, epic_split, retire = st.tabs(["Insert", "Split", "Merge", "Move", "Split an epic", "Remove"])
    with insert, st.form(f"insert_{opp_id}", clear_on_submit=True):
        title = st.text_input("Title")
        statement = st.text_area("Requirement statement")
        epic = st.selectbox("Epic", list(epics), format_func=epic_label)
        after = st.selectbox("Place after", ["(start of the epic)"] + list(brs), format_func=lambda b: label(b) if b in brs else b)
        if _submit("Insert") and title and statement:
            _act(lambda s: svc.insert_br(s, actor(), opp_id, {"title": title, "statement": statement}, epic, None if after not in brs else after))
    with split, st.form(f"split_{opp_id}", clear_on_submit=True):
        original = st.selectbox("Requirement to split", list(brs), format_func=label)
        parts_text = st.text_area("One part per line: Title | Requirement statement")
        if _submit("Split"):
            parts = [{"title": t.strip(), "statement": x.strip()} for t, _, x in (line.partition("|") for line in parts_text.splitlines()) if t.strip() and x.strip()]
            if len(parts) < 2:
                st.error("Give at least two parts.")
            else:
                _act(lambda s: svc.split_br(s, actor(), opp_id, original, parts))
    with merge, st.form(f"merge_{opp_id}", clear_on_submit=True):
        survivor = st.selectbox("Keep this ID", list(brs), format_func=label)
        others = st.multiselect("Merge these into it", list(brs), format_func=label)
        if _submit("Merge") and others and survivor not in others:
            _act(lambda s: svc.merge_brs(s, actor(), opp_id, survivor, others))
    with move, st.form(f"move_{opp_id}", clear_on_submit=True):
        moving = st.selectbox("Requirement", list(brs), format_func=label)
        target = st.selectbox("To epic", list(epics), format_func=epic_label)
        if _submit("Move"):
            _act(lambda s: svc.move_br(s, actor(), opp_id, moving, target))
    with epic_split, st.form(f"epic_split_{opp_id}", clear_on_submit=True):
        source = st.selectbox("Epic to split", list(epics), format_func=epic_label)
        new_name = st.text_input("New epic name")
        moved = st.multiselect("Requirements for the new epic", list(brs), format_func=label)
        if _submit("Split epic") and new_name and moved:
            _act(lambda s: svc.split_epic(s, actor(), opp_id, source, new_name, moved))
    with retire, st.form(f"retire_{opp_id}", clear_on_submit=True):
        gone = st.selectbox("Requirement", list(brs), format_func=label)
        sure = st.checkbox("Confirm: its ID stays retired and is never reused.")
        if _submit("Remove") and sure:
            _act(lambda s: svc.retire_br(s, actor(), opp_id, gone))


def _change_requests(opp_id: str, specialist: bool) -> None:
    with session_scope() as s:
        requests = [{"id": c.id, "proposals": c.diff_summary["proposals"], "rewrites": c.diff_summary.get("rewrites", {}), "document": supplement._label(s.get(models.Document, c.document_id)) if c.document_id else ""}
                    for c in supplement.open_requests(s, opp_id)]
    for request in requests:
        st.warning(f"**Change request from {request['document']}.** Untouched requirements stay exactly as they are; only what you accept changes.")
        unplaced = [p for p in request["proposals"] if p["type"] == supplement.UNPLACED and p["status"] == "pending"]
        if unplaced and specialist:
            cost = supplement.estimated_cost_usd(len(unplaced))
            allowed = cost <= spend.remaining_today()
            if _button(st, f"Place {len(unplaced)} statements with AI (about USD {cost:.3f})", key=f"llm_{request['id']}", disabled=not allowed):
                _act(lambda s: _classify(s, opp_id, request["id"], llm))
        with session_scope() as s:
            targets = supplement.rewrite_targets(s.get(models.ChangeRequest, request["id"]))
        if targets and specialist:
            cost = supplement.rewrite_cost_usd(len(targets))
            st.caption(f"{len(targets)} amended requirements need one clean merged statement. Only these go to the LLM.")
            if _button(st, f"Write merged statements (about USD {cost:.3f})", key=f"rewrite_{request['id']}", disabled=cost > spend.remaining_today()):
                _act(lambda s: _rewrite(s, opp_id, request["id"]))
        for p in request["proposals"]:
            _proposal(opp_id, request["id"], p, specialist, request["rewrites"], bool(targets))
        if specialist and all(p["status"] != "pending" for p in request["proposals"]) and _button(st, "Apply the accepted changes", type="primary", key=f"finish_{request['id']}"):
            _act(lambda s: supplement.finish(s, actor(), opp_id, request["id"]))


def _classify(session, opp_id: str, cr_id: int, llm_module) -> int:
    usage = llm_module.Usage(limit_usd=spend.max_run_usd())
    try:
        return supplement.classify_with_llm(session, actor(), opp_id, cr_id, usage)
    finally:
        spend.record(usage.totals()["est_usd"])
        ops.record_llm(session, opp_id, "classify_supplement", usage)


def _rewrite(session, opp_id: str, cr_id: int) -> dict:
    usage = llm.Usage(limit_usd=spend.max_run_usd())
    try:
        return supplement.rewrite_amendments(session, actor(), opp_id, cr_id, usage)
    finally:
        spend.record(usage.totals()["est_usd"])
        ops.record_llm(session, opp_id, "rewrite_amendment", usage)


def _proposal(opp_id: str, cr_id: int, p: dict, specialist: bool, rewrites: dict, rewrite_waiting: bool) -> None:
    src = p["source"]
    with st.container(border=True):
        st.markdown(f"**{p['id']} · {PROPOSAL_LABEL[p['type']]}** · {src['document']}" + (f", §{src['section']}" if src["section"] else "") + (f", p.{src['page']}" if src.get("page") else "") + f" · *{p['status']}*")
        st.write(p["text"])
        target = p.get("br_id") or p.get("question_id") or p.get("answers")
        if target:
            st.caption(f"Applies to {target}" + (f"; also answers {p['answers']}" if p.get("answers") else ""))
        if p.get("record_update"):
            st.caption("Updates the opportunity record only: " + ", ".join(p["record_update"]))
        if p["status"] != "pending" or not specialist:
            return
        accept, reject = st.columns(2)
        edited = None
        waiting = False
        if p["type"] == supplement.AMENDS:
            rewrite = rewrites.get(p["br_id"])
            waiting = rewrite_waiting and (rewrite is None or p["id"] not in rewrite["covers"])
            if waiting:
                st.caption("Write the merged statement above first.")
            else:
                if rewrite["status"] == "needs_rewording":
                    st.warning(f"{brd_docx.NEEDS_REWORDING}: the AI could not write a clean statement, so this is the plain wording. Edit it if you can.")
                wording = st.text_area("Proposed statement", rewrite["statement"], key=f"word_{cr_id}_{p['id']}_{len(rewrite['statement'])}")
                edited = {"statement": wording} if wording != rewrite["statement"] else None
        if p["type"] == supplement.UNPLACED:
            st.caption("Place it with AI above, or reject it if it changes nothing.")
        if _button(accept, "Accept", key=f"accept_{cr_id}_{p['id']}", disabled=p["type"] == supplement.UNPLACED or waiting):
            _act(lambda s: supplement.decide(s, actor(), opp_id, cr_id, p["id"], True, edited))
        if _button(reject, "Reject", key=f"reject_{cr_id}_{p['id']}"):
            _act(lambda s: supplement.decide(s, actor(), opp_id, cr_id, p["id"], False))


def _registers(opp_id: str, specialist: bool) -> None:
    with session_scope() as s:
        snapshot = store.brd_snapshot(s, opp_id)
    items = {}
    for section in snapshot["section_items"]:
        items.setdefault(section["section"], []).append(section)
    tabs = st.tabs(["Scope", "Objectives", "Business rules", "Data entities", "NFRs", "Constraints & assumptions", "Open questions", "Risks", "Retired", "Withdrawn"])
    def show(tab, rows):
        with tab:
            if rows:
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
            else:
                st.info("Nothing here.")
    with tabs[0]:
        st.markdown("**In scope**")
        show(st.container(), [{"ID": i["item_id"], **i["data"]} for i in items.get("in_scope", [])])
        st.markdown("**Out of scope**")
        show(st.container(), [{"ID": i["item_id"], "Exclusion": i["data"]["exclusion"], "Rationale": i["data"]["rationale"]} for i in items.get("out_of_scope", [])])
    show(tabs[1], [{"ID": i["item_id"], **i["data"]} for i in items.get("objective", [])])
    show(tabs[2], [{"ID": r["item_id"], "Rule": r["text"], "Related BRs": ", ".join(r["br_ids"]), "Superseded by": r["superseded_by"] or ""} for r in snapshot["business_rules"]])
    show(tabs[3], [{"ID": r["item_id"], "Entity": r["text"], "Attributes": ", ".join(r["ext"].get("attributes", [])), "Related BRs": ", ".join(r["br_ids"])} for r in snapshot["entities"]])
    show(tabs[4], [{"ID": r["item_id"], "Category": r["category"], "Expectation": r["text"], "Metric / Target": r["metric_target"]} for r in snapshot["nfrs"]])
    show(tabs[5], [{"ID": r["item_id"], "Type": r["item_type"], "Description": r["text"], "Status": r["status"]} for r in snapshot["constraints_assumptions"]])
    show(tabs[6], [{"ID": r["item_id"], "Question": r["text"], "Related BRs": ", ".join(r["br_ids"]), "Blocking": "Yes" if r["blocking"] else "No", "Audience": r["audience"], "Status": r["status"], "Answer source": r["answer_source"] or ""} for r in snapshot["open_questions"]])
    with tabs[6]:
        if specialist:
            ids = {r["item_id"]: r for r in snapshot["open_questions"]}
            c1, c2, c3 = st.columns([2, 2, 1], vertical_alignment="bottom")
            pick_q = c1.selectbox("Who is this question for?", list(ids), key=f"audience_pick_{opp_id}")
            audience = c2.selectbox("Audience", ["client", "internal"], index=["client", "internal"].index(ids[pick_q]["audience"]), key=f"audience_{opp_id}_{pick_q}")
            if _button(c3, "Save", key=f"audience_save_{opp_id}", disabled=audience == ids[pick_q]["audience"]):
                _act(lambda s: svc.set_question_audience(s, actor(), opp_id, pick_q, audience))
        waiting = [r["item_id"] for r in snapshot["open_questions"] if r["blocking"] and r["status"] == "Open"]
        if waiting and specialist:
            pick = st.selectbox("Acknowledge a blocking question", waiting, key=f"ack_{opp_id}")
            if _button(st, "Acknowledge", key=f"ack_button_{opp_id}"):
                _act(lambda s: svc.acknowledge_question(s, actor(), opp_id, pick))
    show(tabs[9], [{"ID": w["item_id"], "Type": brd_docx.SECTION_LABEL[w["section"]], "Text": w["text"], "Withdrawn by": f"{w['by']} ({w['role']})", "When": w["at"][:16].replace("T", " "), "Reason": w.get("reason") or ""} for w in snapshot["withdrawn"]])
    show(tabs[7], [{"ID": i["item_id"], "Risk": i["data"]["description"], "Probability": i["data"]["probability"], "Impact": i["data"]["impact"], "Mitigation": i["data"]["mitigation"]} for i in items.get("risk", [])])
    show(tabs[8], [{"BR ID": b["br_id"], "Status": b["lifecycle_status"], "Title": b["title"], "Split from": b["split_from"] or "", "Merged into": b["merged_into"] or ""} for b in snapshot["brs"] if b["lifecycle_status"] != "Active"])


def _versions(opp_id: str) -> None:
    with session_scope() as s:
        opp = s.get(models.Opportunity, opp_id)
        code = opp.code
        versions = s.scalars(select(models.ArtifactVersion).where(models.ArtifactVersion.opportunity_id == opp_id, models.ArtifactVersion.artifact_type == "BRD")
                             .order_by(models.ArtifactVersion.version_major.desc(), models.ArtifactVersion.version_minor.desc())).all()
        listing = [{"id": v.id, "label": v.version_label, "status": v.status, "by": f"{v.created_by} ({v.created_role})", "at": v.created_at, "summary": v.change_summary or "",
                    "stability": v.snapshot.get("stability", {}).get("message", ""), "data": brd_docx.render(s, opp_id, v)} for v in versions]
    for v in listing:
        line, button = st.columns([5, 1])
        line.markdown(f"**v{v['label']}** · {v['status']} · {v['by']} · {v['at']:%Y-%m-%d %H:%M}  \n{v['summary']}" + (f"  \n*{v['stability']}*" if v["stability"] else ""))
        if not brd_docx.leak_hits(v["data"], st.session_state.get("active_rates")):
            button.download_button("Open", v["data"], file_name=brd_docx.file_name(code, v["label"]), key=f"version_{v['id']}")
    if len(listing) < 2:
        st.caption("A comparison appears once there are two versions.")
        return
    st.divider()
    st.markdown("**Compare versions**")
    labels = [v["label"] for v in listing]
    c1, c2 = st.columns(2)
    older = c1.selectbox("From", labels, index=1, key=f"compare_from_{opp_id}_{labels[0]}")
    newer = c2.selectbox("To", labels, index=0, key=f"compare_to_{opp_id}_{labels[0]}")
    if older == newer:
        st.caption("Pick two different versions.")
        return
    with session_scope() as s:
        result = brd_compare.compare(s, opp_id, older, newer)
        report = brd_compare.report_docx(s, opp_id, result)
    totals = result["summary"]
    cols = st.columns(7)
    for col, name in zip(cols, ("Unchanged", "Modified", "Added", "Removed", "Split", "Merged")):
        col.metric(name, totals[name])
    cols[6].metric("% unchanged", f"{totals['percent_unchanged']:.0f}%")
    f1, f2 = st.columns(2)
    changed_only = f1.toggle("Changed only", True, key=f"compare_changed_{opp_id}")
    sources = sorted({r["cause"] for r in result["rows"] + result["items"] if r["cause"]})
    source = f2.selectbox("By source", ["All sources"] + sources, key=f"compare_source_{opp_id}")

    def visible(rows):
        return [r for r in rows if (not changed_only or r["status"] != "Unchanged") and source in ("All sources", r["cause"])]

    st.markdown(brd_compare.html_table(visible(result["rows"]), "Requirements"), unsafe_allow_html=True)
    st.markdown(brd_compare.html_table(visible(result["items"]), "Objectives, scope, questions, rules and risks"), unsafe_allow_html=True)
    st.download_button("Download comparison (DOCX, internal)", report, file_name=f"BRD-{code}-R1-compare-v{older}-v{newer}.docx", key=f"compare_download_{opp_id}")


# ---------- estimate and timeline ----------

SCENARIO_LABEL = {"B": "B AI-Augmented", "C": "C AI-First"}


def _rates() -> tuple:
    return st.session_state.get("active_rates"), st.session_state.get("discount_value", 0.0) / 100


def _money(value: float) -> str:
    return f"PHP {value:,.0f}"


def _load(opp_id: str, artifact: str) -> dict | None:
    with session_scope() as s:
        v = ops.current_version(s, opp_id, artifact)
        return {"label": v.version_label, "status": v.status, "outdated": v.outdated, "summary": v.change_summary or "", "by": f"{v.created_by} ({v.created_role})", "snapshot": v.snapshot} if v else None


def _estimate(opp_id: str, state: dict) -> None:
    specialist = wf.can(actor().role, wf.ACT_EDIT_ESTIMATE)
    status = state["status"]
    current = _load(opp_id, "ESTIMATE")
    if status == wf.BRD_APPROVED:
        st.caption("AI-generated estimates and timeline, from the approved BRD. Unchanged requirements keep their sizes, overrides and validation. Calculated by code: no LLM, USD 0.00.")
        if _button(st, "Generate estimates and timeline", type="primary", disabled=not specialist, key=f"generate_estimate_{opp_id}"):
            _act(lambda s: est.generate(s, actor(), opp_id, *_rates()))
    if current is None:
        st.info("The AI-generated estimates and timeline appear here after the BRD is approved.")
        return
    snap = current["snapshot"]
    open_for_edit = specialist and status in (wf.ESTIMATE_GENERATED, wf.ESTIMATE_VALIDATION) or (specialist and status in wf._ESTIMATE_REOPENABLE)
    st.markdown(f"**Estimate v{current['label']}** · {current['status']} · based on BRD v{snap['brd_version']} · {current['by']}  \n{current['summary']}")
    if current["outdated"]:
        st.warning("Outdated: the BRD changed. Generate the update once the new BRD version is approved.")
    if snap.get("delta"):
        _delta_view(snap["delta"])
    columns = st.columns(2)
    for col, s_ in zip(columns, est.SCENARIOS):
        t = snap["totals"][s_]
        col.metric(SCENARIO_LABEL[s_], f"{t['man_days']:,.0f} man-days")
        saving = snap["results"]["estimates"]["reduction_vs_baseline_pct"][s_]
        col.caption(f"{t['months']:.1f} months · {_money(t['price_ex_vat'])} · margin {t['gross_margin_pct']:.0f}% · {saving:.0f}% less effort than the calibration baseline")
    wbs, team, cost, risk_tab, assumptions, validation = st.tabs(["Work breakdown structure", "Team & timeline", "Cost & pricing", "Risks", "Assumptions", "Validate"])
    with wbs:
        _wbs_view(opp_id, current, open_for_edit)
    with team:
        _team_view(opp_id, current, open_for_edit)
    with cost:
        _cost_view(snap)
    with risk_tab:
        rows = [{"Risk": r["title"], "Severity": r["severity"], "Category": r["category"], "Mitigation": r.get("mitigation") or "", "Why": r.get("explanation") or ""} for r in snap["results"]["risks"]]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch") if rows else st.info("No risks triggered.")
    with assumptions:
        _assumptions_view(opp_id, current, open_for_edit)
    with validation:
        _validation_view(opp_id, current, state, specialist)
    st.caption("Internal: this page holds pricing.")


def _delta_view(delta: dict) -> None:
    target = "this file" if delta["to_version"] == "preview" else f"v{delta['to_version']}"
    st.info(f"**What changed from BRD v{delta['from_version']} to {target}.** {delta['unchanged']} requirements unchanged, kept as validated.")
    rows = [{"BR": r["br_id"], "Title": r["title"], "Change": r["change"], "Size": f"{r['size_from'] or '—'} → {r['size_to'] or '—'}",
             "B man-days": f"{(r['man_days_from'] or {}).get('B', 0):,.1f} → {(r['man_days_to'] or {}).get('B', 0):,.1f}", "C man-days": f"{(r['man_days_from'] or {}).get('C', 0):,.1f} → {(r['man_days_to'] or {}).get('C', 0):,.1f}"} for r in delta["rows"]]
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    totals = [{"Scenario": SCENARIO_LABEL[s_], "Man-days": f"{t['from']['man_days']:,.0f} → {t['to']['man_days']:,.0f}", "Months": f"{t['from']['months']:.1f} → {t['to']['months']:.1f}",
               "Price ex VAT": f"{_money(t['from']['price_ex_vat'])} → {_money(t['to']['price_ex_vat'])}"} for s_, t in delta["totals"].items()]
    st.dataframe(pd.DataFrame(totals), hide_index=True, width="stretch")


def _wbs_view(opp_id: str, current: dict, editable: bool) -> None:
    snap, label = current["snapshot"], current["label"]
    rows = [{"BR ID": b, "Changed": "Size" if r["size_overridden"] else "", "Epic": r["epic_id"], "Title": r["title"], "Size": r["size"] or "", "B man-days": r["man_days"]["B"], "C man-days": r["man_days"]["C"], "Validation": "Validated" if r["validated"] else "Needs validation", "Sizing": est.RULE_SIZED if r.get("rule_sized") else "", "Priced": "Yes" if r["priced"] else "No: suggested"} for b, r in snap["rows"].items()]
    frame = pd.DataFrame(rows)
    styled = _highlight(frame).format({"B man-days": "{:,.1f}", "C man-days": "{:,.1f}"})
    locked = [c for c in frame.columns if c != "Size"] if editable else list(frame.columns)
    edited = st.data_editor(styled, hide_index=True, width="stretch", disabled=locked, key=f"wbs_{opp_id}_{label}", column_config={"Size": st.column_config.SelectboxColumn(options=list(svc.SIZES), required=True)})
    if editable:
        sizes = {r["BR ID"]: r["Size"] for r, was in zip(edited.to_dict("records"), rows) if r["Size"] != was["Size"]}
        if _button(st, f"Save size changes ({len(sizes)} unsaved)", type="primary", disabled=not sizes, key=f"save_sizes_{opp_id}_{label}"):
            _act(lambda s: est.edit(s, actor(), opp_id, label, sizes=sizes, rates=_rates()[0], discount=_rates()[1]))
    pick = st.selectbox("Show activities for", ["All"] + list(snap["rows"]) + ["Foundation"], key=f"wbs_filter_{opp_id}", format_func=lambda b: b if b in ("All", "Foundation") else f"{b} · {snap['rows'][b]['title']}")
    activities = [r for r in est.wbs_rows(snap) if pick in ("All", r["br_id"])]
    day = units.available()["Man-days"]
    st.dataframe(pd.DataFrame([{"Epic": r["epic"], "BR": r["br_id"], "Feature": r["feature"], "Role": r["role"], "Activity": r["activity"], "Baseline hours": round(r["baseline"], 1), "B man-days": round(day.value(r["B"]), 1), "C man-days": round(day.value(r["C"]), 1), "Override": "●" if r["overridden"] else ""} for r in activities]), hide_index=True, width="stretch")
    if editable and activities:
        names = {r["key"]: f"{r['br_id']} · {r['role']} · {r['activity']} ({r['baseline']:.1f} h)" for r in activities}
        with st.form(f"activity_override_{opp_id}_{label}", clear_on_submit=True):
            st.markdown("**Override activity hours** (with a reason; AI factors still apply)")
            key = st.selectbox("Activity", list(names), format_func=names.get)
            hours = st.number_input("Baseline hours", 0.0, 5000.0, 0.0, 1.0)
            reason = st.text_input("Reason")
            if _submit("Save override"):
                _act(lambda s: est.edit(s, actor(), opp_id, label, activity_hours={key: {"hours": hours, "reason": reason}}, rates=_rates()[0], discount=_rates()[1]))
    for key, value in snap["inputs"]["activity_hours"].items():
        st.caption(f"Override {key}: {value['hours']:g} hours. {value['reason']} ({value['by']})")


def _team_view(opp_id: str, current: dict, editable: bool) -> None:
    snap, label = current["snapshot"], current["label"]
    timeline = snap["results"]["timeline"]
    for s_ in est.SCENARIOS:
        st.markdown(f"**{SCENARIO_LABEL[s_]}**: {timeline[s_]['total_months']:.1f} months, {timeline[s_]['start_date']} to {timeline[s_]['end_date']}")
        team_rows = [{"Role": r, "FTE": float(v)} for r, v in timeline[s_]["team"].items()]
        edited = st.data_editor(pd.DataFrame(team_rows), hide_index=True, disabled=["Role"] if editable else ["Role", "FTE"], key=f"team_{opp_id}_{label}_{s_}")
        if editable:
            changes = {r["Role"]: float(r["FTE"]) for r, was in zip(edited.to_dict("records"), team_rows) if float(r["FTE"]) != was["FTE"]}
            if _button(st, f"Save {s_} team ({len(changes)} unsaved)", disabled=not changes, key=f"save_team_{opp_id}_{label}_{s_}"):
                _act(lambda s: est.edit(s, actor(), opp_id, label, team={s_: changes}, rates=_rates()[0], discount=_rates()[1]))
        st.dataframe(pd.DataFrame([{"Phase": p["label"], "Start": p["start_date"], "End": p["end_date"], "Months": round(p["months"], 1)} for p in timeline[s_]["phases"] if p["months"] > 0]), hide_index=True, width="stretch")


def _cost_view(snap: dict) -> None:
    costs = snap["results"]["costing"]["scenarios"]
    lines = (("Labor", "labor"), ("AI tooling", "ai_tooling"), ("Infrastructure", "infrastructure"), ("Warranty reserve", "warranty_reserve"), ("Contingency", "contingency"), ("Delivery cost", "delivery_cost"),
             ("Price excluding VAT", "price_ex_vat"), ("VAT", "vat"), ("Price including VAT", "price_incl_vat"), ("Price floor", "price_floor"))
    st.dataframe(pd.DataFrame([{"Item": label, **{SCENARIO_LABEL[s_]: _money(costs[s_][key]) for s_ in est.SCENARIOS}} for label, key in lines]), hide_index=True, width="stretch")
    st.dataframe(pd.DataFrame([{"Item": "Gross margin", **{SCENARIO_LABEL[s_]: f"{costs[s_]['gross_margin_pct']:.1f}%" for s_ in est.SCENARIOS}}, {"Item": "Target margin", **{SCENARIO_LABEL[s_]: f"{costs[s_]['target_gross_margin_pct']:.1f}%" for s_ in est.SCENARIOS}}]), hide_index=True, width="stretch")
    if all("ai_tooling_lines" in costs[s_] for s_ in est.SCENARIOS):
        st.markdown("**AI tooling cost and net savings** (prices and usage are illustrative, from `config/tooling_profiles.yaml`)")
        st.caption(f"Tooling profile: {snap['results']['estimates'].get('tooling_profile', '')}. Change it under Assumptions.")
        st.dataframe(pd.DataFrame([{"Item": row, **{SCENARIO_LABEL[s_]: _money(value) for s_, value in values.items()}} for row, values in est.tooling_table(costs)]), hide_index=True, width="stretch")
        compared = snap["results"]["estimates"].get("tooling_comparison_c_expected", {})
        if compared and all("net_savings" in v for v in compared.values()):
            st.caption("AI-First at the Expected profile, by tooling profile:")
            st.dataframe(pd.DataFrame([{"Tooling profile": name, "AI tooling": _money(v["ai_tooling"]), "Net savings": _money(v["net_savings"])} for name, v in compared.items()]), hide_index=True, width="stretch")
    else:
        st.info("This estimate was saved before AI tooling had its own lines. Its total above still counts. Recalculating a draft shows the lines and net savings.")
    source = snap["results"]["costing"].get("rates_source", "illustrative")
    st.caption(f"Priced with {source.upper()} rates. Rate values are never stored; only their source and a hash are.")


def _assumptions_view(opp_id: str, current: dict, editable: bool) -> None:
    snap, label = current["snapshot"], current["label"]
    saved = snap["inputs"]["assumptions"]
    cfg = estimation.configs(saved)
    profiles, tooling = list(cfg["ai_scenarios"]["profiles"]), list(cfg["tooling"]["profiles"])
    st.caption("All assumptions and productivity adjustments. Estimated productivity assumptions are not empirically validated. Saving recalculates by code.")
    with st.form(f"assumptions_{opp_id}_{label}"):
        c1, c2 = st.columns(2)
        profile = c1.selectbox("Productivity profile", profiles, index=profiles.index(cfg["ai_scenarios"]["active_profile"]), disabled=not editable)
        tool = c2.selectbox("AI tooling profile", tooling, index=tooling.index(cfg["tooling"]["active_profile"]), disabled=not editable)
        d1, d2, d3 = st.columns(3)
        low, high = cfg["schedule"]["uat_weeks_range"]
        uat = d1.number_input("Final UAT (weeks)", int(low), int(high), int(cfg["schedule"]["uat_weeks"]), disabled=not editable)
        pilot = d2.number_input("Pilot (weeks)", 0, 26, int(cfg["schedule"]["pilot_weeks"]), disabled=not editable)
        warranty = d3.number_input("Warranty (months)", 0, 60, int(saved.get("warranty_months") or snap["results"]["estimates"]["warranty"]["months"]), disabled=not editable)
        e1, e2, e3 = st.columns(3)
        contingency = e1.number_input("Contingency", 0.0, 0.5, float(cfg["commercial"]["contingency_rate"]), 0.01, disabled=not editable)
        margin = e2.number_input("Target gross margin", 0.0, 0.9, float(cfg["commercial"]["target_gross_margin"]), 0.01, disabled=not editable)
        validated = e3.checkbox("AI-First savings validated", bool(cfg["commercial"]["ai_first_validated"]), disabled=not editable)
        if _submit("Save assumptions", disabled=not editable):
            now = {"productivity_profile": profile, "tooling_profile": tool, "uat_weeks": uat, "pilot_weeks": pilot, "warranty_months": warranty, "contingency_rate": contingency, "target_gross_margin": margin, "ai_first_validated": validated}
            was = {"productivity_profile": cfg["ai_scenarios"]["active_profile"], "tooling_profile": cfg["tooling"]["active_profile"], "uat_weeks": cfg["schedule"]["uat_weeks"], "pilot_weeks": cfg["schedule"]["pilot_weeks"],
                   "warranty_months": snap["results"]["estimates"]["warranty"]["months"], "contingency_rate": cfg["commercial"]["contingency_rate"], "target_gross_margin": cfg["commercial"]["target_gross_margin"], "ai_first_validated": cfg["commercial"]["ai_first_validated"]}
            changes = {k: v for k, v in now.items() if v != was[k]}
            _act(lambda s: est.edit(s, actor(), opp_id, label, assumptions=changes, rates=_rates()[0], discount=_rates()[1]))


def _validation_view(opp_id: str, current: dict, state: dict, specialist: bool) -> None:
    snap, label, status = current["snapshot"], current["label"], state["status"]
    if status == wf.ESTIMATE_GENERATED and _button(st, "Start validation", type="primary", disabled=not specialist, key=f"start_estimate_{opp_id}"):
        _act(lambda s: est.start_validation(s, actor(), opp_id))
    validating = specialist and status == wf.ESTIMATE_VALIDATION
    waiting = sum(1 for r in snap["rows"].values() if not r["validated"])
    st.markdown(f"{len(snap['rows']) - waiting} of {len(snap['rows'])} rows validated")
    rule_sized = [b for b, r in snap["rows"].items() if r.get("rule_sized")]
    if rule_sized:
        st.warning(f"{est.RULE_SIZED}: {', '.join(rule_sized)}. Their size came from a rule, not the AI or a person. Check it in the work breakdown structure.")
    if validating and waiting and _button(st, "Validate all remaining rows", key=f"validate_rows_{opp_id}_{label}"):
        _act(lambda s: est.validate_rows(s, actor(), opp_id))
    st.markdown("**Validate the generated estimates and timeline**")
    for key, text_ in est.CHECKLIST.items():
        ticked = st.checkbox(text_, bool(snap["checklist"].get(key)), disabled=not validating, key=f"check_{opp_id}_{label}_{key}")
        if ticked != bool(snap["checklist"].get(key)):
            _act(lambda s, k=key, v=ticked: est.tick(s, actor(), opp_id, k, v))
    with session_scope() as s:
        blockers = est.approval_blockers(s, opp_id)
        summary = est.summary_docx(s, opp_id)
        code = s.get(models.Opportunity, opp_id).code
    if status == wf.ESTIMATE_VALIDATION:
        if _button(st, "Approve estimate", type="primary", disabled=not specialist or bool(blockers), key=f"approve_estimate_{opp_id}"):
            _act(lambda s: est.approve(s, actor(), opp_id, expected_row_version=state["row_version"]))
        if blockers:
            st.caption("Approval needs: " + " ".join(blockers))
    st.download_button("Download estimate summary (DOCX, INTERNAL — CONTAINS PRICING)", summary, file_name=est.file_name(code, label), key=f"estimate_summary_{opp_id}_{label}")
    if wf.can(actor().role, wf.ACT_ADMIN):
        with st.form(f"re_estimate_{opp_id}", clear_on_submit=True):
            st.markdown("**Re-estimate all** (Admin): discards every override")
            reason = st.text_input("Reason")
            if _submit("Re-estimate all"):
                _act(lambda s: est.re_estimate_all(s, actor(), opp_id, reason, *_rates()))


# ---------- proposal ----------

def _proposal_step(opp_id: str, state: dict) -> None:
    status = state["status"]
    role = actor().role
    bdm = wf.can(role, wf.ACT_TRIGGER_COMMERCIAL)
    current = _load(opp_id, "PROPOSAL")
    if status in (wf.ESTIMATE_APPROVED, wf.PROPOSAL_GENERATED):
        _trigger_form(opp_id, state, bdm)
    if current is None:
        if status != wf.ESTIMATE_APPROVED:
            st.info("The BDM triggers the proposal once the BRD and the estimate are approved and current.")
        return
    snap = current["snapshot"]
    st.markdown(f"**Proposal v{current['label']}** · {current['status']} · scenario {SCENARIO_LABEL[snap['scenario']]} · BRD v{snap['brd_version']}, estimate v{snap['estimate_version']}  \n{current['summary']}")
    if current["outdated"]:
        st.warning("Outdated: the BRD or the estimate changed. Generate it again once they are approved.")
    checks = snap["checks"]
    if checks["leak_hits"]:
        st.error("Client copy blocked: internal data in " + ", ".join(sorted({h["section"] for h in checks["leak_hits"]})) + ".")
    consistency = checks["consistency"]
    if consistency["ok"]:
        st.success(f"Client copy: leak check and consistency check passed ({consistency['checked']} items checked against the record).")
    else:
        st.error("The client copy differs from the record: " + "; ".join(consistency["mismatches"] + consistency["placeholders"]) + ".")
    with session_scope() as s:
        code = s.get(models.Opportunity, opp_id).code
        client, internal = prop.client_copy(s, opp_id), prop.internal_copy(s, opp_id)
    c1, c2 = st.columns(2)
    if not checks["leak_hits"]:
        c1.download_button("Download client copy", client, file_name=prop.file_name(code, current["label"]), key=f"proposal_client_{opp_id}_{current['label']}")
    c2.download_button("Download internal review copy", internal, file_name=prop.file_name(code, current["label"], True), key=f"proposal_internal_{opp_id}_{current['label']}")
    if status in (wf.PROPOSAL_GENERATED, wf.PROPOSAL_REVIEW) and bdm:
        with st.expander("Re-upload an edited client copy"):
            st.caption("Wording edits become the next version. A changed price, VAT, duration, milestone, scope item, warranty or name is refused: commercial changes go through the estimate.")
            upload = st.file_uploader("Edited proposal (DOCX)", type=["docx"], key=f"proposal_upload_{opp_id}_{current['label']}")
            if upload is not None and _button(st, "Check and save", key=f"proposal_upload_save_{opp_id}_{current['label']}"):
                _act(lambda s: prop.reupload(s, actor(), opp_id, upload.getvalue(), *_rates()))
    can_review = wf.can(role, wf.ACT_APPROVE_PROPOSAL)
    if status == wf.PROPOSAL_GENERATED and _button(st, "Start the proposal review", disabled=not can_review, key=f"start_review_{opp_id}"):
        _act(lambda s: prop.start_review(s, actor(), opp_id))
    if status == wf.PROPOSAL_REVIEW:
        if current["status"] == "Approved":
            if bdm:
                when = st.date_input("Submission date", value=None, key=f"submitted_{opp_id}")
                if _button(st, "Mark as submitted", type="primary", disabled=not when, key=f"submit_{opp_id}"):
                    _act(lambda s: prop.mark_submitted(s, actor(), opp_id, when.isoformat()))
            else:
                st.caption("Approved. The BDM marks it as submitted.")
        else:
            _review_controls(opp_id, can_review)


def _trigger_form(opp_id: str, state: dict, bdm: bool) -> None:
    latest = _load(opp_id, "ESTIMATE")
    if latest is None:
        return
    results = latest["snapshot"]["results"]
    recommendation = results["recommendation"]
    default = recommendation.get("recommended_scenario") or "B"
    st.markdown(f"**Recommendation: {SCENARIO_LABEL[default]}.** {recommendation.get('rule', '')} {recommendation.get('upside') or ''}")
    scenario = st.selectbox("Scenario", est.SCENARIOS, index=est.SCENARIOS.index(default), format_func=SCENARIO_LABEL.get, disabled=not bdm, key=f"proposal_scenario_{opp_id}")
    highs = {r["id"]: r["title"] for r in results["risks"] if r["severity"] == "high"}
    acknowledged = st.multiselect("Acknowledge every high-severity risk", list(highs), format_func=highs.get, disabled=not bdm, key=f"proposal_acks_{opp_id}")
    with session_scope() as s:
        try:
            brd, estimate = prop._approved_versions(s, opp_id)
            consent = risks.consent_required(prop.record_for(s, opp_id, brd, estimate))
            cost = prop.estimated_cost_usd(s, opp_id, scenario)
            blocked = None
        except wf.InvalidTransition as e:
            consent, cost, blocked = False, 0.0, str(e)
    if blocked:
        st.warning(blocked)
        return
    consent_by, consent_date = "", ""
    if consent:
        st.warning("The chosen AI tooling sends data outside the Philippines: record the client or legal consent.")
        c1, c2 = st.columns(2)
        consent_by = c1.text_input("Consent given by", key=f"consent_by_{opp_id}", disabled=not bdm)
        when = c2.date_input("Consent date", value=None, key=f"consent_date_{opp_id}", disabled=not bdm)
        consent_date = when.isoformat() if when else ""
    st.caption("Served from the cache: USD 0.00." if cost == 0 else f"The narrative is written by the LLM: about USD {cost:.3f}. Team, timeline and pricing come from the approved estimate, in code.")
    label = "Regenerate proposal" if state["status"] == wf.PROPOSAL_GENERATED else "Generate proposal"
    if _button(st, label, type="primary" if label == "Generate proposal" else "secondary", disabled=not bdm or cost > spend.remaining_today(), key=f"generate_proposal_{opp_id}"):
        usage = llm.Usage(limit_usd=spend.max_run_usd())

        def run(s):
            try:
                return prop.trigger(s, actor(), opp_id, scenario, acknowledged, consent_by, consent_date, usage, *_rates())
            finally:
                spend.record(usage.totals()["est_usd"])
                ops.record_llm(s, opp_id, "generate_proposal", usage)
        _act(run)


def _review_controls(opp_id: str, can_review: bool) -> None:
    with session_scope() as s:
        blockers = prop.approval_blockers(s, opp_id)
        version = ops.current_version(s, opp_id, "PROPOSAL")
        reviews = [(a.approver_name, a.approver_role, a.decision) for a in prop._reviews(s, version)]
    st.caption("Needs both a BDM and a Legal/Management approval on this version." + (" So far: " + "; ".join(f"{n} ({r}): {d}" for n, r, d in reviews) if reviews else ""))
    if blockers:
        st.caption("Approval needs: " + " ".join(blockers))
    comment = st.text_input("Comment (required to reject or return)", key=f"proposal_comment_{opp_id}", disabled=not can_review)
    approve, ret, reject = st.columns(3)
    if _button(approve, "Approve proposal", type="primary", disabled=not can_review or bool(blockers), key=f"proposal_approve_{opp_id}"):
        _act(lambda s: prop.decide(s, actor(), opp_id, "Approved", comment or None))
    if _button(ret, "Return", disabled=not can_review, key=f"proposal_return_{opp_id}"):
        _act(lambda s: prop.decide(s, actor(), opp_id, "Returned", comment))
    if _button(reject, "Reject", disabled=not can_review, key=f"proposal_reject_{opp_id}"):
        _act(lambda s: prop.decide(s, actor(), opp_id, "Rejected", comment))


# ---------- award ----------

def _award(opp_id: str, state: dict) -> None:
    bdm = wf.can(actor().role, wf.ACT_TRIGGER_COMMERCIAL)
    with session_scope() as s:
        award = dict(s.get(models.Opportunity, opp_id).award)
    if award:
        st.markdown(f"**{award['outcome']}** · {award['date']} · recorded by {award['by']}" + (f"  \n{award['note']}" if award.get("note") else ""))
    status = state["status"]
    if status == wf.AWARDED:
        st.success("Awarded: the SOW and MSA can be triggered next.")
    elif status == wf.CLOSED:
        st.info("Closed — Not Awarded. This opportunity can be viewed, not changed.")
    if status != wf.PROPOSAL_SUBMITTED:
        if not award:
            st.info("The BDM records the award decision after the proposal is submitted.")
        return
    with st.form(f"award_{opp_id}", clear_on_submit=True):
        outcome = st.selectbox("Outcome", prop.OUTCOMES, disabled=not bdm)
        when = st.date_input("Decision date", value=None, disabled=not bdm)
        note = st.text_input("Note", disabled=not bdm)
        if _submit("Record the decision", disabled=not bdm):
            _act(lambda s: prop.record_award(s, actor(), opp_id, outcome, when.isoformat() if when else "", note))


# ---------- contracts ----------

def _contracts(opp_id: str, state: dict) -> None:
    bdm = wf.can(actor().role, wf.ACT_TRIGGER_COMMERCIAL)
    status = state["status"]
    with session_scope() as s:
        opp = s.get(models.Opportunity, opp_id)
        fields, row_version, code = dict(opp.commercial.get("contract") or {}), opp.row_version, opp.code
        unlocked = opp.award.get("outcome") == prop.AWARDED
    if not unlocked:
        st.info("Only an Awarded decision unlocks the SOW and MSA.")
        return
    with st.form(f"contract_fields_{row_version}"):
        st.markdown("**Contract details** (filled into the SOW and MSA, by code)")
        values = {}
        c1, c2 = st.columns(2)
        for i, (key, label) in enumerate(contracts.CONTRACT_FIELDS.items()):
            values[key] = (c1 if i % 2 == 0 else c2).text_input(label, fields.get(key, ""), disabled=not bdm)
        if _submit("Save contract details", disabled=not bdm):
            _act(lambda s: contracts.update_fields(s, actor(), opp_id, values, row_version))
    cost = contracts.estimated_cost_usd()
    can_generate = bdm and status in (wf.AWARDED, wf.CONTRACTS_GENERATED, wf.LEGAL_REVIEW, wf.CONTRACT_READY)
    st.caption(f"The SOW comes from the approved versions, by code; the LLM writes only a short objectives summary (about USD {cost:.4f}). The MSA is the company template filled by code: no LLM touches it.")
    if can_generate and _button(st, "Generate SOW and MSA", type="primary", key=f"generate_contracts_{opp_id}"):
        usage = llm.Usage(limit_usd=spend.max_run_usd())

        def run(s):
            try:
                result = contracts.generate(s, actor(), opp_id, usage, *_rates())
                st.session_state[f"msa_message_{opp_id}"] = result["msa_message"]
                return result
            finally:
                spend.record(usage.totals()["est_usd"])
                ops.record_llm(s, opp_id, "sow_summary", usage)
        _act(run)
    sow, msa = _load(opp_id, "SOW"), _load(opp_id, "MSA")
    if sow is None:
        return
    with session_scope() as s:
        sow_file = contracts.sow_bytes(s, opp_id)
        msa_file = contracts.msa_bytes(s, opp_id, fresh=False) if msa else None
    snap = sow["snapshot"]
    st.markdown(f"**SOW v{sow['label']}** · {sow['status']} · from proposal v{snap['proposal_version']}, BRD v{snap['brd_version']}, estimate v{snap['estimate_version']}")
    if sow["outdated"]:
        st.warning("Outdated: something it was built from changed. Generate it again.")
    if snap.get("leak_hits"):
        st.error("The SOW holds internal data: " + ", ".join(sorted({h["section"] for h in snap["leak_hits"]})) + ".")
    else:
        st.download_button(f"Download SOW-{code}-R1-v{sow['label']}.docx", sow_file, file_name=f"SOW-{code}-R1-v{sow['label']}.docx", key=f"sow_{opp_id}_{sow['label']}")
    if msa is None:
        st.error(contracts.MSA_UNAVAILABLE)
    else:
        st.markdown(f"**MSA v{msa['label']}** · {msa['status']} · {contracts.MSA_MARK}")
        if msa["snapshot"]["missing"]:
            st.warning("Still to fill, highlighted in the file: " + ", ".join(msa["snapshot"]["missing"]) + ". Add them above and generate again.")
        if msa_file is None:
            st.error(contracts.MSA_UNAVAILABLE)
        else:
            st.download_button(f"Download MSA-{code}-R1-v{msa['label']}.docx", msa_file, file_name=f"MSA-{code}-R1-v{msa['label']}.docx", key=f"msa_{opp_id}_{msa['label']}")
    if status == wf.CONTRACTS_GENERATED:
        allowed = wf.can(actor().role, wf.ACT_APPROVE_MSA)
        if _button(st, "Start the legal and management review", type="primary" if allowed else "secondary", disabled=not allowed or msa is None, key=f"start_legal_{opp_id}"):
            _act(lambda s: contracts.start_review(s, actor(), opp_id))
        if not allowed:
            st.caption(f"A {wf.LEGAL} starts the review.")


def _final(opp_id: str, state: dict) -> None:
    status = state["status"]
    msa = _load(opp_id, "MSA")
    with session_scope() as s:
        parts = contracts.status_of_parts(s, opp_id)
        check = contracts.consistency(s, opp_id, fresh=False)
    if status == wf.CONTRACT_READY:
        st.success("Contract-Ready: all approvals are in and the documents agree. Any later change reopens the review.")
    if check["ok"]:
        st.success(f"Consistency check passed: proposal, SOW, MSA and record agree ({check['checked']} items).")
    else:
        st.error("Consistency check: " + "; ".join(check["mismatches"]) + ".")
    if msa is not None:
        st.markdown("**Clauses for review**")
        reviewer = wf.can(actor().role, wf.ACT_APPROVE_MSA) and status == wf.LEGAL_REVIEW
        for i, flag in enumerate(msa["snapshot"]["flags"]):
            ticked = st.checkbox(f"{flag['category']}: {flag['heading']}", bool(flag["reviewed"]), disabled=not reviewer, key=f"clause_{opp_id}_{msa['label']}_{i}")
            if ticked != bool(flag["reviewed"]):
                _act(lambda s, k=i, v=ticked: contracts.set_clause_reviewed(s, actor(), opp_id, k, v))
        if not msa["snapshot"]["flags"]:
            st.caption("No clause headings matched the review list. Legal reviews the whole document.")
    st.markdown("**Final approvals** (all required, any order)")
    with session_scope() as s:
        blockers = {part: contracts.approval_blockers(s, opp_id, part, fresh=False) for part in contracts.PARTS}
    for part, (label, action, artifact) in contracts.PARTS.items():
        info = parts[part]
        line, button = st.columns([4, 1])
        line.markdown(f"{'✅' if info['approved'] else '⬜'} **{label}** · {artifact} v{info['version'] or '—'}" + (f" · approved by {info['by']}" if info["by"] else ""))
        if status == wf.LEGAL_REVIEW and not info["approved"] and _button(button, "Approve", key=f"final_{part}_{opp_id}", disabled=not wf.can(actor().role, action) or bool(blockers[part])):
            _act(lambda s, p=part: contracts.approve(s, actor(), opp_id, p))
    if status == wf.LEGAL_REVIEW:
        shown = sorted({b for items in blockers.values() for b in items})
        if shown:
            st.caption("Approval needs: " + " ".join(shown))
        comment = st.text_input("Comment (required to return)", key=f"final_comment_{opp_id}")
        if _button(st, "Return for changes", key=f"final_return_{opp_id}"):
            _act(lambda s: contracts.return_for_changes(s, actor(), opp_id, comment))


def _audit(opp_id: str) -> None:
    with session_scope() as s:
        rows = [{"When": a.occurred_at.strftime("%Y-%m-%d %H:%M:%S"), "Who": a.actor_name, "Role": a.actor_role, "Action": a.action, "Entity": a.entity_id or a.entity_type or "", "Version": a.version or ""}
                for a in s.scalars(select(models.AuditLog).where(models.AuditLog.opportunity_id == opp_id).order_by(models.AuditLog.id.desc()))]
    if not rows:
        st.info("Nothing yet.")
        return
    actions = sorted({r["Action"] for r in rows})
    pick = st.multiselect("Filter by action", actions, key=f"audit_filter_{opp_id}")
    st.dataframe(pd.DataFrame([r for r in rows if not pick or r["Action"] in pick]), hide_index=True, width="stretch")


