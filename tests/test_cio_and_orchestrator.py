from datetime import datetime, timedelta, timezone

import pytest

from agents.cio import CIOAgent, weighted_score
from agents.risk import EXTREME_VOL_FLAG
from agents.technical import FREE_FALL_FLAG
from database import Database
from main import Orchestrator
from models import Action, AgentReport
from risk_manager import RiskManager
from tests.conftest import FakeLLM, make_ohlcv, make_snapshot


def reports(f=90, t=85, s=60, r=80, tflags=(), rflags=()):
    return {
        "fundamental": AgentReport("fundamental", f, (0, 100)),
        "technical": AgentReport("technical", t, (0, 100), metrics={"atr_14": 2.0}, flags=list(tflags)),
        "sentiment": AgentReport("sentiment", s, (-100, 100)),
        "risk": AgentReport("risk", r, (0, 100), flags=list(rflags)),
    }


def llm_buy(score=60, size=9999.0):
    return FakeLLM({
        "ticker": "TEST", "action": "BUY", "final_score": score, "confidence_level": "HIGH",
        "justification": "Fundamentales sólidos y tendencia alcista.",
        "execution_parameters": {"suggested_position_size_eur": size, "stop_loss_price": 1.0, "take_profit_target": 2.0},
    })


PLAN = RiskManager().build_plan(100.0, 2.0)


def test_weighted_score_formula():
    # 0.35*80 + 0.30*70 + 0.20*60 + 0.15*60 = 70
    assert weighted_score(reports()) == pytest.approx(70.0)


def test_llm_buy_but_execution_params_come_from_risk_manager():
    d = CIOAgent(llm_buy()).decide("TEST", reports(), PLAN)
    assert d.action is Action.BUY
    out = d.to_output_json()
    assert out["execution_parameters"] == {
        "suggested_position_size_eur": 700.0, "stop_loss_price": 96.0, "take_profit_target": 108.0,
    }
    assert set(out) == {"ticker", "action", "final_score", "confidence_level", "justification", "execution_parameters"}


@pytest.mark.parametrize("kw", [{"tflags": [FREE_FALL_FLAG]}, {"rflags": [EXTREME_VOL_FLAG]}])
def test_veto_blocks_buy_regardless_of_fundamentals(kw):
    d = CIOAgent(llm_buy()).decide("TEST", reports(f=100, **kw), PLAN)
    assert d.action is Action.HOLD
    assert d.suggested_position_size_eur == 0
    assert d.vetoes


def test_llm_score_adjustment_is_clamped():
    d = CIOAgent(llm_buy(score=100)).decide("TEST", reports(), PLAN)
    assert d.final_score == 85   # 70 + 15


def test_untradeable_plan_blocks_buy():
    plan = RiskManager().build_plan(100.0, 2.0, cash_available_eur=100)
    d = CIOAgent(llm_buy()).decide("TEST", reports(), plan)
    assert d.action is Action.HOLD


def test_deterministic_fallback_without_llm():
    d = CIOAgent(FakeLLM(fail=True)).decide("TEST", reports(f=10, t=10, s=-80, r=20), PLAN)
    assert d.action is Action.SELL
    assert any("modo determinista" in n for n in d.guardrail_notes)


def test_anti_overtrading_cooldown():
    recent = [{"timestamp": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), "recommended_action": "BUY"}]
    d = CIOAgent(llm_buy()).decide("TEST", reports(), PLAN, recent)
    assert d.action is Action.HOLD


def test_wash_sale_warning():
    recent = [{"timestamp": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(), "recommended_action": "SELL"}]
    d = CIOAgent(llm_buy()).decide("TEST", reports(), PLAN, recent)
    assert d.action is Action.BUY
    assert any("dos meses" in n for n in d.guardrail_notes)


def test_justification_truncated_to_50_words():
    llm = llm_buy()
    llm.response["justification"] = "palabra " * 80
    d = CIOAgent(llm).decide("TEST", reports(), PLAN)
    assert len(d.justification.split()) <= 50


class StubFetcher:
    def __init__(self, snap):
        self.snap = snap

    def fetch(self, ticker):
        return self.snap


def test_orchestrator_end_to_end(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    snap = make_snapshot(make_ohlcv(drift=0.0015, vol=0.009))
    orch = Orchestrator(fetcher=StubFetcher(snap), db=db, llm=FakeLLM(fail=True))
    res = orch.analyze("test")
    assert set(res.reports) == {"fundamental", "technical", "sentiment", "risk"}
    rows = db.last_signals()
    assert len(rows) == 1 and rows[0]["ticker"] == "TEST"
    assert rows[0]["recommended_action"] == res.decision.action.value
    assert rows[0]["stop_loss_calculated"] == pytest.approx(res.plan.stop_loss_price)
    # La segunda ejecución reutiliza market_data (UPSERT) y aplica el cooldown.
    res2 = orch.analyze("TEST")
    if res.decision.action is not Action.HOLD:
        assert res2.decision.action is Action.HOLD
    import sqlite3
    with sqlite3.connect(db.path) as c:
        assert c.execute("SELECT COUNT(*) FROM market_data").fetchone()[0] == len(snap.ohlcv)
        assert c.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0] == 1
