"""The example's cases: the spending requests the app is seeded with, and what should happen."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type CaseName = Literal["single", "full"]
type Decision = Literal["approve", "reject_limit", "reject_budget"]

APPROVAL_LIMIT = 500
"""The robot's policy, passed to `run()` as the `approval_limit` input Variable; the app never shows it."""

LIMIT_REASON = "Over the approval limit"
BUDGET_REASON = "Budget exhausted"


@dataclass(frozen=True)
class Row:
    """One spending request, and the decision the Process should take on it."""

    request_id: int
    priority: int
    requester: str
    amount: int
    expected_decision: Decision


@dataclass(frozen=True)
class Case:
    """The requests the app is seeded with, its budget, and the budget expected at the end."""

    rows: tuple[Row, ...]
    budget: int
    expected_budget: int

    def by_priority(self) -> list[Row]:
        return sorted(self.rows, key=lambda row: row.priority)


# Request ids don't follow priority, so the list's sort order is what the Process relies on.
_ANA = Row(104, 1, "Ana", 300, "approve")
_BEN = Row(102, 2, "Ben", 800, "reject_limit")
_CLEO = Row(105, 3, "Cleo", 450, "approve")
_DAN = Row(101, 4, "Dan", 400, "reject_budget")
_EVA = Row(103, 5, "Eva", 200, "approve")

CASES: dict[CaseName, Case] = {
    "single": Case(rows=(_ANA,), budget=1000, expected_budget=700),
    "full": Case(rows=(_DAN, _BEN, _EVA, _ANA, _CLEO), budget=1000, expected_budget=50),
}
