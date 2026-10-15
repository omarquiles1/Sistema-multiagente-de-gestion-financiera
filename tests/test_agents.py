import numpy as np
import pandas as pd

from agents.fundamental import FundamentalAgent
from agents.risk import EXTREME_VOL_FLAG, RiskAgent
from agents.sentiment import SentimentAgent, lexicon_score
from agents.technical import FREE_FALL_FLAG, TechnicalAgent, atr, rsi
from tests.conftest import FakeLLM, make_ohlcv, make_snapshot


def test_rsi_bounds_and_atr_positive():
    df = make_ohlcv()
    r = rsi(df["close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()
    assert (atr(df["high"], df["low"], df["close"]).dropna() > 0).all()


def test_technical_uptrend_scores_high():
    rep = TechnicalAgent().run(make_snapshot(make_ohlcv(drift=0.002, vol=0.008)))
    assert rep.error is None
    assert rep.score > 60
    assert "Tendencia alcista" in rep.flags
    for key in ("rsi_14", "macd", "sma_50", "sma_200", "bb_upper", "atr_14"):
        assert key in rep.metrics


def test_technical_free_fall_flag():
    up = make_ohlcv(n=400, drift=0.001, vol=0.01, seed=1)
    crash = make_ohlcv(n=104, drift=-0.006, vol=0.015, seed=2, start=float(up["close"].iloc[-1]))
    crash.index = pd.bdate_range(up.index[-1] + pd.offsets.BDay(), periods=len(crash))
    rep = TechnicalAgent().run(make_snapshot(pd.concat([up, crash])))
    assert FREE_FALL_FLAG in rep.flags
    assert rep.score < 30


def test_technical_insufficient_history_is_neutral():
    rep = TechnicalAgent().run(make_snapshot(make_ohlcv(n=30)))
    assert rep.error and rep.score == 50


def test_fundamental_quality_company():
    rep = FundamentalAgent().run(make_snapshot())
    assert rep.score >= 75
    assert rep.metrics["debt_equity"] == 0.4


def test_fundamental_weak_company():
    info = {"sector": "Industrials", "trailingPE": -5, "earningsGrowth": -0.4,
            "profitMargins": -0.1, "debtToEquity": 450, "freeCashflow": -1e8, "marketCap": 1e9}
    rep = FundamentalAgent().run(make_snapshot(info=info))
    assert rep.score <= 15
    assert "Free Cash Flow negativo" in rep.flags


def test_fundamental_eps_from_income_statement():
    income = pd.DataFrame({pd.Timestamp("2025-06-30"): [12.0], pd.Timestamp("2024-06-30"): [10.0]},
                          index=["Diluted EPS"])
    snap = make_snapshot(info={"sector": "Technology"})
    snap.income_stmt = income
    m = FundamentalAgent.extract_metrics(snap)
    assert m["eps_growth_yoy"] == 0.2


def test_risk_extreme_volatility_flag():
    rep = RiskAgent().run(make_snapshot(make_ohlcv(vol=0.05)))
    assert EXTREME_VOL_FLAG in rep.flags
    assert rep.score < 40


def test_risk_calm_asset():
    rep = RiskAgent().run(make_snapshot(make_ohlcv(drift=0.0008, vol=0.007)))
    assert EXTREME_VOL_FLAG not in rep.flags
    assert rep.score >= 70
    assert rep.metrics["beta_vs_sp500"] is not None


def test_sentiment_uses_llm_and_clamps():
    llm = FakeLLM({"sentiment_score": 250, "macro_environment": "NEUTRAL", "summary": "a\nb"})
    rep = SentimentAgent(llm).run(make_snapshot())
    assert rep.score == 100 and rep.metrics["mode"] == "llm"
    assert "<titulares>" in llm.calls[0]["user"]


def test_sentiment_fallback_lexicon():
    rep = SentimentAgent(FakeLLM(fail=True)).run(
        make_snapshot(headlines=["Company misses estimates, shares plunge", "Downgrade after lawsuit"])
    )
    assert rep.score < 0 and rep.metrics["mode"] == "lexicon_fallback"


def test_lexicon_neutral_without_news():
    assert lexicon_score([]) == 0


def test_normalization():
    from models import AgentReport
    assert AgentReport("x", 75, (0, 100)).normalized() == 50
    assert AgentReport("x", -40, (-100, 100)).normalized() == -40
