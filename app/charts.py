"""Timeline chart (POC v2). One chart per staffing view, sharing one time axis across the full chart width."""
from __future__ import annotations

import altair as alt
import pandas as pd

PHASE_BARS = {"requirements": "Requirements", "architecture": "Architecture", "development": "Development", "sit": "SIT",
              "testing": "Testing", "uat": "Final UAT", "pilot": "Pilot", "deployment": "Go-live"}
PHASE_COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
LABEL_ROOM_DAYS = 30     # room after the last bar for its label


def gantt_rows(timelines: dict, views: tuple) -> pd.DataFrame:
    return pd.DataFrame([{"View": v, "Phase": PHASE_BARS[p["phase"]], "Start": p["start_date"], "End": p["end_date"],
                          "Months": round(p["months"], 2), "Bottleneck": p["bottleneck"]}
                         for v in views for p in timelines[v]["phases"] if p["months"] > 0])


def gantt(rows: pd.DataFrame, view: str, title: str, legend: bool, width: int | str = "container") -> alt.LayerChart:
    """Every bar carries its phase name; the legend shows on the first chart only, so the others stay aligned."""
    order = list(PHASE_BARS.values())
    end = pd.Timestamp(rows["End"].max()) + pd.Timedelta(days=LABEL_ROOM_DAYS)
    domain = [rows["Start"].min(), end.strftime("%Y-%m-%d")]
    base = alt.Chart(rows[rows["View"] == view]).encode(y=alt.Y("Phase:N", sort=order, axis=None))
    bars = base.mark_bar(cornerRadius=3, size=14).encode(
        x=alt.X("Start:T", title=None, scale=alt.Scale(domain=domain), axis=alt.Axis(format="%b %Y", tickCount="month", gridOpacity=0.3)),
        x2="End:T",
        color=alt.Color("Phase:N", scale=alt.Scale(domain=order, range=PHASE_COLORS),
                        legend=alt.Legend(orient="top", title=None, columns=8) if legend else None),
        tooltip=["Phase", "Start", "End", "Months", "Bottleneck"])
    labels = base.mark_text(align="left", dx=4, fontSize=11).encode(x="End:T", text="Phase:N")
    return (bars + labels).properties(title=title, height=190, width=width)
