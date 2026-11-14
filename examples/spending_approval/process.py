"""The spending-approval Process: approve or reject each pending request, in priority order.

Approve a request if its amount is within the approval limit and the remaining budget; else
reject it as over the limit if it is; else reject it for budget. The approval limit is an
input Variable. Amounts and the budget are read off the page; the Process never computes them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from playwright.async_api import expect

from examples.spending_approval.cases import BUDGET_REASON, LIMIT_REASON
from reparo_playwright_bridge import CheckContext, EventContext, Process


def _int(v: Mapping[str, object], name: str) -> int:
    """An int Variable, as conditions read it."""
    return cast("int", v[name])


def _within_limit_and_budget(v: Mapping[str, object]) -> bool:
    amount = _int(v, "amount")
    return amount <= _int(v, "approval_limit") and amount <= _int(v, "remaining_budget")


def _over_the_limit(v: Mapping[str, object]) -> bool:
    return _int(v, "amount") > _int(v, "approval_limit")


async def _open_reasons(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button", name="Reason").click()


async def _pick_reason(ctx: EventContext, reason: str) -> None:
    await ctx.page.get_by_role("option", name=reason).click()
    await expect(ctx.page.get_by_role("button", name="Reason")).to_contain_text(reason)


async def _click_reject(ctx: EventContext) -> None:
    await ctx.page.get_by_role("button", name="Reject").click()
    await expect(ctx.page.locator("#status")).to_have_text("Rejected")


def build_process(expected_budget: int) -> Process:
    """The Process, with a Completion check that the final budget is `expected_budget`.

    The expectation is closed over rather than passed as a Variable, so Reparo never sees it.
    """
    p = Process("spending_approval", variables={"approval_limit": int})

    with p.activity("Open the spending requests"):

        @p.event("Open the requests page")
        async def open_requests(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/requests")

        @p.event("Read the pending requests")
        async def read_pending(ctx: EventContext) -> None:
            # In displayed order (the list is sorted by priority); never re-sorted here.
            ids = await ctx.page.locator(
                'tr[data-status="pending"] .request-id'
            ).all_inner_texts()
            ctx.vars["pending"] = [int(i) for i in ids]

    with p.for_each("each_request", over="pending", as_="request_id"):
        with p.activity("Open the request"):

            @p.event("Open the request")
            async def open_request(ctx: EventContext) -> None:
                request_id = _int(ctx.vars, "request_id")
                await ctx.page.get_by_role(
                    "link", name=f"Open request {request_id}"
                ).click()
                await expect(ctx.page.locator("h1")).to_have_text(
                    f"Request {request_id}"
                )

            @p.event("Read the request")
            async def read_request(ctx: EventContext) -> None:
                ctx.vars["amount"] = int(await ctx.page.locator("#amount").inner_text())
                ctx.vars["remaining_budget"] = int(
                    await ctx.page.locator("#remaining-budget").inner_text()
                )

        with (
            p.if_(
                "decide",
                _within_limit_and_budget,
                label="amount within limit and budget",
            ),
            p.activity("Approve the request"),
        ):

            @p.event("Click Approve")
            async def click_approve(ctx: EventContext) -> None:
                await ctx.page.get_by_role("button", name="Approve").click()
                await expect(ctx.page.locator("#status")).to_have_text("Approved")

        with (
            p.elif_(_over_the_limit, label="amount over the limit"),
            p.activity("Reject over limit"),
        ):

            @p.event("Open the Reason dropdown")
            async def open_reason_limit(ctx: EventContext) -> None:
                await _open_reasons(ctx)

            @p.event(f"Pick {LIMIT_REASON}")
            async def pick_reason_limit(ctx: EventContext) -> None:
                await _pick_reason(ctx, LIMIT_REASON)

            @p.event("Click Reject")
            async def click_reject_limit(ctx: EventContext) -> None:
                await _click_reject(ctx)

        with p.else_(), p.activity("Reject for budget"):

            @p.event("Open the Reason dropdown")
            async def open_reason_budget(ctx: EventContext) -> None:
                await _open_reasons(ctx)

            @p.event(f"Pick {BUDGET_REASON}")
            async def pick_reason_budget(ctx: EventContext) -> None:
                await _pick_reason(ctx, BUDGET_REASON)

            @p.event("Click Reject")
            async def click_reject_budget(ctx: EventContext) -> None:
                await _click_reject(ctx)

        with p.activity("Return to the list"):

            @p.event("Go back to the requests")
            async def back_to_list(ctx: EventContext) -> None:
                await ctx.page.get_by_role("link", name="Back to requests").click()
                await expect(ctx.page.locator("h1")).to_have_text("Spending requests")

    with p.activity("Record the final budget"):

        @p.event("Read the final budget")
        async def read_final_budget(ctx: EventContext) -> None:
            _ = await ctx.page.goto("/requests")
            ctx.vars["remaining_budget"] = int(
                await ctx.page.locator("#remaining-budget").inner_text()
            )

    @p.completion_check("the remaining budget is as expected")
    async def budget_matches(ctx: CheckContext) -> tuple[bool, str | None]:
        remaining = ctx.vars.get("remaining_budget")
        if remaining == expected_budget:
            return True, None
        return False, f"expected {expected_budget}, got {remaining}"

    return p
