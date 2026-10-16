from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from agents.technical import TechnicalAgent, key_support
from main import apply_live_price
from models import Action, AgentReport, CIODecision, Confidence
from notifier import format_signal, support_from_reports
from sniper import LiveQuote, Sniper, build_quote, market_is_open, market_of
from tests.conftest import make_ohlcv, make_snapshot

MADRID = ZoneInfo("Europe/Madrid")


def madrid(h, m=0, day=16):   # 16/10/2026 es viernes
    return datetime(2026, 10, day, h, m, tzinfo=MADRID)


# ------------------------------------------------------------- soporte clave
def test_key_support_picks_nearest_level_below_price():
    levels = {"SMA200": 90.0, "SMA50": 97.0, "Bollinger": 101.0, "Mínimo 30": float("nan")}
    assert key_support(100.0, levels) == ("SMA50", 97.0)
    assert key_support(80.0, levels) is None


def test_technical_report_includes_soporte_clave():
    rep = TechnicalAgent().run(make_snapshot(make_ohlcv(drift=0.001, vol=0.01)))
    m = rep.metrics
    assert m["soporte_clave"] is not None and m["soporte_clave"] <= m["price"]
    assert m["soporte_clave_tipo"] in {
        "SMA200", "SMA50", "Banda inferior de Bollinger", "Mínimo de 30 sesiones", "Mínimo de 52 semanas",
    }
    assert m["soporte_clave_distancia_pct"] <= 0
    assert "soporte_clave" in rep.to_dict()["metrics"]


def test_recent_ipo_without_sma200_still_analyzed():
    rep = TechnicalAgent().run(make_snapshot(make_ohlcv(n=120)))   # < 200 sesiones
    assert rep.error is None and "Histórico insuficiente para SMA200" in rep.flags
    assert rep.metrics["soporte_clave"] is not None


def test_support_ignores_todays_low_on_crash_day():
    df = make_ohlcv(drift=0.001, vol=0.01)
    df.iloc[-1, df.columns.get_loc("low")] = df["close"].iloc[-1] * 0.5   # mínimo intradía absurdo
    m = TechnicalAgent().run(make_snapshot(df)).metrics
    assert m["soporte_clave"] > df["close"].iloc[-1] * 0.5


def test_signal_message_prints_support():
    d = CIODecision("NVDA", Action.HOLD, 10, Confidence.LOW, "x", 0, 90.0, 120.0)
    tech = AgentReport("technical", 50, (0, 100), metrics={
        "soporte_clave": 95.5, "soporte_clave_tipo": "SMA200", "soporte_clave_distancia_pct": -4.2, "divisa": "USD",
    })
    text = format_signal(d.to_output_json(), support=support_from_reports({"technical": tech}))
    assert "🛡️ Soporte clave cercano: <b>95.50 USD</b> (SMA200, -4.2%)" in text
    assert "Soporte" not in format_signal(d.to_output_json())


# --------------------------------------------------------------- precio vivo
def test_apply_live_price_replaces_today_or_appends():
    today = pd.Timestamp.now().normalize()
    df = pd.DataFrame({"open": [10, 10], "high": [11, 11], "low": [9, 9], "close": [10, 10], "volume": [1, 1]},
                      index=[today - pd.Timedelta(days=1), today], dtype=float)
    out = apply_live_price(df, 8.0)
    assert len(out) == 2 and out["close"].iloc[-1] == 8.0 and out["low"].iloc[-1] == 8.0
    appended = apply_live_price(df.iloc[:1], 8.0)
    assert len(appended) == 2 and appended.index[-1] == today


# ------------------------------------------------------------ horario mercado
@pytest.mark.parametrize("ticker,when,expected", [
    ("SAN.MC", madrid(10), True), ("NVDA", madrid(10), False),
    ("SAN.MC", madrid(16), True), ("NVDA", madrid(16), True),
    ("SAN.MC", madrid(18), False), ("NVDA", madrid(21, 59), True), ("NVDA", madrid(22, 5), False),
    ("SAN.MC", madrid(10, day=17), False),             # sábado
])
def test_market_hours(ticker, when, expected):
    assert market_is_open(ticker, when) is expected


def test_market_of():
    assert market_of("TSK.MC") == "EU" and market_of("SPCX") == "US" and market_of("BTC-EUR") == "CRYPTO"


# ----------------------------------------------------------------- cotización
def daily_series(n=40, start=100.0, end_day="2026-10-16"):
    idx = pd.bdate_range(end=end_day, periods=n)
    return pd.Series(np.linspace(start, start * 1.1, n), index=idx)


def test_build_quote_uses_previous_session_close():
    daily = daily_series()   # incluye la vela de hoy, que debe ignorarse
    minute = pd.Series([104.0, 103.0], index=pd.DatetimeIndex([madrid(10, 0), madrid(10, 1)]))
    q = build_quote("SAN.MC", minute, daily)
    assert q.price == 103.0
    assert q.prev_close == pytest.approx(daily.iloc[-2])
    assert q.change_pct == pytest.approx((103.0 / daily.iloc[-2] - 1) * 100)
    assert q.rsi_now < q.rsi_prev      # el precio vivo cae respecto a una tendencia alcista


def test_build_quote_needs_data():
    assert build_quote("X", pd.Series(dtype=float), daily_series()) is None


# ---------------------------------------------------------------- francotirador
def quote(change_pct=-6.0, rsi_now=35.0, rsi_prev=40.0, ticker="NVDA", when=None):
    return LiveQuote(ticker, 100 * (1 + change_pct / 100), when or madrid(17), 100.0, rsi_now, rsi_prev)


class Result:
    def __init__(self, fund_score=80.0, fund_error=None):
        self.reports = {
            "fundamental": AgentReport("fundamental", fund_score, (0, 100), error=fund_error),
            "technical": AgentReport("technical", 40, (0, 100), metrics={
                "soporte_clave": 91.0, "soporte_clave_tipo": "SMA200", "soporte_clave_distancia_pct": -3.2, "divisa": "USD",
            }),
        }
        self.decision = CIODecision("NVDA", Action.HOLD, 20, Confidence.MEDIUM, "Pánico de mercado.", 0, 85.0, 110.0)


def make_sniper(quotes, result=None):
    sent, calls = [], []

    def analyze(ticker, live_price=None, dip_mode=False):
        calls.append((ticker, live_price, dip_mode))
        if isinstance(result, Exception):
            raise result
        return result or Result()

    return Sniper(analyze, sent.append, fetch=lambda tickers: {q.ticker: q for q in quotes if q.ticker in tickers}), sent, calls


def test_drop_with_intact_fundamental_fires_opportunity():
    q = quote(-6.0)
    sniper, sent, calls = make_sniper([q])
    events = sniper.scan(["NVDA"], now=madrid(17))
    assert calls == [("NVDA", q.price, True)]               # precio de 1 minuto + modo Buy the Dip
    assert events[0].opportunity
    assert sent[0].startswith("🚨 <b>OPORTUNIDAD DE CAÍDA DETECTADA: NVDA ha caído un 6.0%. El fundamental sigue intacto.</b>")
    assert "🛡️ Soporte clave cercano" in sent[0] and "Vela de 1 min" in sent[0]


def test_opportunity_calls_hook_only_when_intact():
    rearmed = []
    sniper, _, _ = make_sniper([quote(-6.0)])
    sniper.on_opportunity = rearmed.append
    sniper.scan(["NVDA"], now=madrid(17))
    weak, _, _ = make_sniper([quote(-6.0)], Result(fund_score=40))
    weak.on_opportunity = rearmed.append
    weak.scan(["NVDA"], now=madrid(17))
    assert rearmed == ["NVDA"]


def test_drop_with_weak_fundamental_is_plain_crash_alert():
    sniper, sent, _ = make_sniper([quote(-7.0)], Result(fund_score=40))
    events = sniper.scan(["NVDA"], now=madrid(17))
    assert not events[0].opportunity and sent[0].startswith("📉 <b>CAÍDA FUERTE: NVDA -7.0%")


def test_etf_without_fundamentals_is_not_opportunity():
    sniper, sent, _ = make_sniper([quote(-6.0)], Result(fund_score=50, fund_error="sin datos"))
    sniper.scan(["NVDA"], now=madrid(17))
    assert "sin datos fundamentales" in sent[0]


def test_rsi_cross_below_30_fires():
    sniper, sent, _ = make_sniper([quote(-2.0, rsi_now=28, rsi_prev=33)])
    events = sniper.scan(["NVDA"], now=madrid(17))
    assert len(events) == 1 and "RSI(14) cruza" in sent[0]


@pytest.mark.parametrize("q", [quote(-4.9), quote(-2.0, rsi_now=25, rsi_prev=27)])   # ya estaba bajo 30
def test_no_trigger(q):
    sniper, sent, calls = make_sniper([q])
    assert sniper.scan(["NVDA"], now=madrid(17)) == [] and calls == []


def test_dedupes_per_day_and_refires_on_deeper_drop():
    sniper, sent, _ = make_sniper([quote(-6.0)])
    sniper.scan(["NVDA"], now=madrid(17))
    sniper.scan(["NVDA"], now=madrid(17, 5))
    assert len(sent) == 1
    sniper.fetch = lambda tickers: {"NVDA": quote(-11.0, when=madrid(17, 10))}
    sniper.scan(["NVDA"], now=madrid(17, 10))
    assert len(sent) == 2                                   # nuevo tramo de −10 %


def test_skips_closed_markets_and_stale_bars():
    stale = quote(-8.0, when=madrid(17, day=15))            # vela de ayer
    sniper, sent, _ = make_sniper([stale, quote(-8.0, ticker="SAN.MC", when=madrid(19))])
    assert sniper.scan(["NVDA", "SAN.MC"], now=madrid(19)) == []   # SAN.MC cerrado a las 19:00


def test_analysis_failure_still_alerts():
    sniper, sent, _ = make_sniper([quote(-9.0)], ValueError("yfinance caído"))
    sniper.scan(["NVDA"], now=madrid(17))
    assert "El análisis completo falló" in sent[0]


def test_fetch_failure_is_swallowed():
    def boom(tickers):
        raise RuntimeError("rate limit")

    sniper = Sniper(lambda *a, **k: None, lambda t: None, fetch=boom)
    assert sniper.scan(["NVDA"], now=madrid(17)) == []
