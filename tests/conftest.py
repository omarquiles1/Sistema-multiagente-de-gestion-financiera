import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm_client import LLMUnavailable  # noqa: E402
from models import MarketSnapshot  # noqa: E402


def make_ohlcv(n=504, drift=0.0005, vol=0.012, seed=0, start=100.0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, vol, n)
    close = start * np.exp(np.cumsum(rets))
    idx = pd.bdate_range("2024-10-01", periods=n)
    spread = close * vol
    return pd.DataFrame(
        {"open": close, "high": close + spread, "low": close - spread, "close": close, "volume": 1e6},
        index=idx,
    )


def make_snapshot(ohlcv=None, info=None, headlines=None, fx=1.0, currency="USD"):
    ohlcv = make_ohlcv() if ohlcv is None else ohlcv
    return MarketSnapshot(
        ticker="TEST",
        ohlcv=ohlcv,
        info=info if info is not None else {
            "sector": "Technology", "trailingPE": 25.0, "earningsGrowth": 0.15,
            "profitMargins": 0.30, "debtToEquity": 40.0, "freeCashflow": 6e10, "marketCap": 1.5e12,
            "currency": currency,
        },
        income_stmt=None,
        cashflow=None,
        news_headlines=headlines if headlines is not None else ["Company beats estimates", "Record revenue"],
        currency=currency,
        fx_to_eur=fx,
        fed_rate=4.25,
        ecb_rate=2.0,
        benchmark_close=ohlcv["close"] * 0.9,
    )


class FakeLLM:
    """LLM simulado: devuelve una respuesta fija o falla."""

    def __init__(self, response=None, fail=False):
        self.response, self.fail, self.calls = response, fail, []

    def json_completion(self, system, user, schema, effort="medium", max_tokens=16000):
        self.calls.append({"system": system, "user": user, "schema": schema})
        if self.fail:
            raise LLMUnavailable("sin LLM (test)")
        return self.response


@pytest.fixture
def snapshot():
    return make_snapshot()
