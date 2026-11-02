"""Deal Advisor (POC v3): password, then a self-declared name and role, then the opportunities and their workspace. The screens are in workspace_ui."""
from __future__ import annotations

import hmac
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import costing, workspace_ui, workflow  # noqa: E402
from app.config import env, load_dotenv_if_present  # noqa: E402

load_dotenv_if_present()
st.set_page_config(page_title="AI Deal-to-Delivery Advisor (POC)", layout="wide")


def gate() -> bool:
    expected = env("APP_PASSWORD")
    if not expected:
        st.error("APP_PASSWORD is not configured. The app is locked.")
        return False
    if st.session_state.get("authed"):
        return True
    st.title("AI Deal-to-Delivery Advisor")
    with st.form("login"):
        pw = st.text_input("Password", type="password")
        if st.form_submit_button("Enter"):
            if hmac.compare_digest(pw.encode(), expected.encode()):
                st.session_state.authed = True
                st.rerun()
            else:
                time.sleep(1)
                st.error("Wrong password.")
    return False


if not gate():
    st.stop()
workspace_ui.require_identity()


def active_rates() -> costing.RateTable:
    """Session memory only: uploaded rates are never saved to disk, logs or the database."""
    if "rates" not in st.session_state:
        st.session_state.rates = costing.illustrative_rates()
        local = env("RATES_FILE")   # local testing only; never set in the container
        if local and Path(local).is_file():
            try:
                st.session_state.rates = costing.parse_rates_csv(Path(local).read_bytes())
            except costing.RatesError as e:
                st.session_state.rates_error = f"RATES_FILE: {e} Illustrative rates are used."
    return st.session_state.rates


with st.sidebar:
    workspace_ui.identity_sidebar()
    rates = active_rates()
    if workflow.can(workspace_ui.actor().role, workflow.ACT_ADMIN):
        with st.expander("Admin: Rates", expanded=False):
            st.markdown(f"Active rates: **{'ILLUSTRATIVE' if rates.source == 'illustrative' else 'UPLOADED'}**")
            if rates.defaulted:
                st.warning(f"Not in the file, so the illustrative rate is used: {', '.join(rates.defaulted)}.")
            st.caption("CSV columns: role_name, published_rate_php_per_hour, applied_rate_php_per_hour (optional). Uploaded rates stay in this session's memory only.")
            up = st.file_uploader("Upload published rates (CSV)", type=["csv"], key="rates_upload")
            if up is not None and st.session_state.get("rates_upload_id") != up.file_id:
                st.session_state.rates_upload_id = up.file_id
                try:
                    st.session_state.rates = costing.parse_rates_csv(up.getvalue())
                    st.session_state.pop("rates_error", None)
                except costing.RatesError as e:
                    st.session_state.rates_error = f"{e} Illustrative rates are kept."
                    st.session_state.rates = costing.illustrative_rates()
                st.rerun()
            if st.session_state.get("rates_error"):
                st.error(st.session_state.rates_error)
            if st.button("Reset to illustrative rates"):
                st.session_state.rates = costing.illustrative_rates()
                st.session_state.pop("rates_error", None)
                st.rerun()
            st.session_state.discount_value = st.number_input("Discount on published rates (%)", 0.0, 50.0, st.session_state.get("discount_value", 0.0), 1.0)
            blended = costing.blended_rates(st.session_state.rates, st.session_state.discount_value / 100)
            st.dataframe(pd.DataFrame([{"Role": r, "Published (blended)": round(v["published"]), "Applied (blended)": round(v["applied"])} for r, v in blended.items()]), hide_index=True)

st.title("AI Deal-to-Delivery Advisor")
st.warning("Decision support. AI outputs are drafts: people approve requirements, estimates, proposals and contracts. Internal pricing is never client-facing.")
workspace_ui.render(rates)
