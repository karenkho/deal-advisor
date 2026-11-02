"""Display units for effort (POC v2). Calculations stay in hours; this only converts what internal views show."""
from __future__ import annotations

from dataclasses import dataclass

from app.config import estimation_config, load_yaml


@dataclass(frozen=True)
class Unit:
    name: str
    short: str
    hours: float
    decimals: int

    def value(self, hours: float) -> float:
        return hours / self.hours

    def show(self, hours: float) -> str:
        return f"{self.value(hours):,.{self.decimals}f} {self.short}"

    def number(self, hours: float) -> str:
        return f"{self.value(hours):,.{self.decimals}f}"


HOURS = Unit("Hours", "h", 1.0, 0)


def available() -> dict[str, Unit]:
    """Man-days (default), hours, person-months, from the calibrated capacity settings."""
    day = float(estimation_config()["capacity"]["hours_per_manday"])
    month = float(load_yaml("schedule.yaml")["hours_per_fte_month"])
    return {"Man-days": Unit("Man-days", "man-days", day, 0), "Hours": HOURS,
            "Person-months": Unit("Person-months", "person-months", month, 1)}
