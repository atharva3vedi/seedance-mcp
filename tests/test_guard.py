from datetime import datetime

import pytest

from seedance_mcp.guard import GuardError, Ledger, SpendGuard, period_start
from seedance_mcp.pricing import PriceBook


@pytest.fixture
def guard(make_settings):
    def make(**settings):
        s = make_settings(**settings)
        return SpendGuard(s, PriceBook(), Ledger(s.ledger_path))

    return make


def spend(guard, usd, record_id="x"):
    guard.ledger.upsert(
        {
            "id": record_id,
            "kind": "image",
            "status": "succeeded",
            "created": guard.clock(),
            "usd": usd,
        }
    )


def test_cheap_requests_go_through(guard):
    g = guard(ask_above_usd=0.5, budget_usd=20)
    decision = g.evaluate(0.09)
    assert not decision.needs_approval and not decision.reasons


def test_expensive_request_needs_approval(guard):
    decision = guard(ask_above_usd=0.5).evaluate(1.17)
    assert decision.needs_approval
    assert "ask-first limit" in decision.reasons[0]


def test_crossing_the_budget_needs_approval_even_when_cheap(guard):
    g = guard(ask_above_usd=5, budget_usd=10)
    spend(g, 9.95)
    decision = g.evaluate(0.09)
    assert decision.needs_approval
    assert "past your $10.00 budget" in decision.reasons[0]


def test_hard_cap_blocks_and_off_mode_never_asks(guard):
    g = guard(hard_cap_usd=10, approval="off")
    spend(g, 9.5)
    assert g.evaluate(1.0).blocked
    assert not g.evaluate(0.4).needs_approval


def test_failed_tasks_cost_nothing(guard):
    g = guard()
    g.ledger.upsert({"id": "t", "status": "failed", "created": g.clock(), "est_usd": 3})
    assert g.spent() == 0


def test_single_approval_is_bound_to_the_exact_request(guard):
    g = guard()
    ticket = g.open_single(1.17, "fingerprint-a", "a clip")
    with pytest.raises(GuardError, match="different settings"):
        g.redeem(ticket.id, 1.17, "fingerprint-b")
    g.redeem(ticket.id, 1.17, "fingerprint-a")
    with pytest.raises(GuardError, match="unknown or expired"):
        g.redeem(ticket.id, 1.17, "fingerprint-a")


def test_batch_approval_is_a_dollar_envelope(guard):
    g = guard()
    batch = g.open_batch(2.5, calls=2, summary="two clips", confirmed=False)
    with pytest.raises(GuardError, match="never confirmed"):
        g.redeem(batch.id, 1.2, "f")
    g.confirm_batch(batch.id)
    g.redeem(batch.id, 1.2, "f")
    with pytest.raises(GuardError, match="more than"):
        g.redeem(batch.id, 2.0, "f")
    approval = g.redeem(batch.id, 1.2, "f")
    g.refund(approval, 1.2)
    assert approval.calls_left == 1
    g.redeem(batch.id, 1.2, "f")
    with pytest.raises(GuardError, match="used up"):
        g.redeem(batch.id, 0.01, "f")


def test_approvals_expire(guard):
    g = guard()
    now = [1_000_000.0]
    g.clock = lambda: now[0]
    ticket = g.open_single(1.0, "f", "clip")
    now[0] += 16 * 60
    with pytest.raises(GuardError, match="expired"):
        g.redeem(ticket.id, 1.0, "f")


def test_ledger_is_shared_between_processes(tmp_path):
    a, b = Ledger(tmp_path / "l.jsonl"), Ledger(tmp_path / "l.jsonl")
    a.upsert({"id": "1", "usd": 1.0})
    b.upsert({"id": "2", "usd": 2.0})
    a.upsert({"id": "1", "status": "succeeded"})
    assert {r["id"] for r in a.all()} == {"1", "2"}
    assert b.get("1") == {"id": "1", "usd": 1.0, "status": "succeeded"}


def test_period_start():
    wednesday = datetime(2026, 10, 14, 15, 30).timestamp()
    assert datetime.fromtimestamp(period_start("day", wednesday)) == datetime(2026, 10, 14)
    assert datetime.fromtimestamp(period_start("week", wednesday)) == datetime(2026, 10, 12)
    assert datetime.fromtimestamp(period_start("month", wednesday)) == datetime(2026, 10, 1)


def test_ask_text_is_in_dollars(guard):
    g = guard(budget_usd=50)
    spend(g, 14.2)
    text = g.ask_text("a clip", 1.17, 1.17, g.evaluate(1.17))
    assert "Spend about $1.17 on a clip?" in text
    assert "$14.20 spent of your $50.00 budget. After this: $15.37." in text
