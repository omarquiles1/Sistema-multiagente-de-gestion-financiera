"""Agente Técnico: momento del precio. Python puro (pandas / pandas-ta), SIN LLM."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from agents.base import BaseAgent
from models import AgentReport, MarketSnapshot

FREE_FALL_FLAG = "Caída libre por debajo de SMA200"

try:  # pandas-ta es la implementación preferida
    import pandas_ta as ta
except Exception:  # pragma: no cover - fallback si la librería no carga
    ta = None


# ---------------------------------------------------------------------------
# Indicadores. Se usa pandas-ta; si no está disponible, implementaciones
# equivalentes en pandas puro (mismas fórmulas: Wilder para RSI y ATR).
# ---------------------------------------------------------------------------
def _rma(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    if ta is not None:
        return ta.rsi(close, length=n)
    delta = close.diff()
    gain = _rma(delta.clip(lower=0), n)
    loss = _rma(-delta.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(loss.notna())


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    if ta is not None:
        m = ta.macd(close, fast=fast, slow=slow, signal=signal)
        cols = {c[: c.index("_")]: c for c in m.columns}
        return pd.DataFrame(
            {"macd": m[cols["MACD"]], "hist": m[cols["MACDh"]], "signal": m[cols["MACDs"]]}
        )
    line = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "hist": line - sig, "signal": sig})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    if ta is not None:
        b = ta.bbands(close, length=n, std=k)
        pick = lambda prefix: b[[c for c in b.columns if c.startswith(prefix)][0]]  # noqa: E731
        return pd.DataFrame({"lower": pick("BBL"), "mid": pick("BBM"), "upper": pick("BBU")})
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=1)  # igual que pandas-ta
    return pd.DataFrame({"lower": mid - k * sd, "mid": mid, "upper": mid + k * sd})


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    if ta is not None:
        return ta.atr(high, low, close, length=n)
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return _rma(tr, n)


def sma(close: pd.Series, n: int) -> pd.Series:
    return ta.sma(close, length=n) if ta is not None else close.rolling(n).mean()


def _last(s: pd.Series | None) -> float:
    if s is None or s.dropna().empty:
        return float("nan")
    return float(s.dropna().iloc[-1])


# ---------------------------------------------------------------------------
class TechnicalAgent(BaseAgent):
    name = "technical"
    scale = (0.0, 100.0)

    def analyze(self, snapshot: MarketSnapshot) -> AgentReport:
        df = snapshot.ohlcv
        if len(df) < 60:
            raise ValueError(f"Histórico insuficiente ({len(df)} sesiones)")
        close, high, low = df["close"], df["high"], df["low"]

        price = float(close.iloc[-1])
        rsi14 = _last(rsi(close, 14))
        m = macd(close)
        macd_v, macd_sig, macd_hist = _last(m["macd"]), _last(m["signal"]), _last(m["hist"])
        sma50_s, sma200_s = sma(close, 50), sma(close, 200)
        sma50, sma200 = _last(sma50_s), _last(sma200_s)
        bb = bollinger(close)
        bb_lower, bb_mid, bb_upper = _last(bb["lower"]), _last(bb["mid"]), _last(bb["upper"])
        bb_pct = (price - bb_lower) / (bb_upper - bb_lower) if bb_upper > bb_lower else 0.5
        atr14 = _last(atr(high, low, close, 14))
        ret20 = float(close.iloc[-1] / close.iloc[-21] - 1) if len(close) > 21 else 0.0

        sma200_valid = sma200_s.dropna()
        sma200_slope = (
            float(sma200_valid.iloc[-1] / sma200_valid.iloc[-21] - 1) if len(sma200_valid) > 21 else float("nan")
        )

        score = 50.0
        flags: list[str] = []

        # --- Tendencia (±30)
        has_sma200 = math.isfinite(sma200)
        above_200 = has_sma200 and price > sma200
        if has_sma200:
            score += 15 if above_200 else -15
            score += 7 if sma50 > sma200 else -7
        else:
            flags.append("Histórico insuficiente para SMA200")
        if math.isfinite(sma50):
            score += 8 if price > sma50 else -8

        if has_sma200 and above_200 and sma50 > sma200:
            flags.append("Tendencia alcista")
        elif has_sma200 and not above_200 and sma50 < sma200:
            flags.append("Tendencia bajista")

        # Caída libre: bajo la SMA200, SMA200 descendente y caída marcada.
        if (
            has_sma200 and not above_200
            and math.isfinite(sma200_slope) and sma200_slope < 0
            and (price < sma200 * 0.95 or ret20 < -0.10)
        ):
            flags.append(FREE_FALL_FLAG)
            score -= 10

        # --- Momento MACD (±10)
        score += 5 if macd_hist > 0 else -5
        if macd_v > macd_sig and macd_v > 0:
            score += 5
        elif macd_v < macd_sig and macd_v < 0:
            score -= 5

        # --- RSI (±10)
        if rsi14 > 70:
            score -= 10
            flags.append("Sobrecompra (RSI>70)")
        elif rsi14 < 30:
            score += 10 if above_200 else 3   # sobreventa en tendencia alcista = oportunidad
            flags.append("Sobreventa (RSI<30)")
        elif 45 <= rsi14 <= 65:
            score += 5
        elif 30 <= rsi14 < 45:
            score += 2

        # --- Bollinger (±5)
        if bb_pct > 1:
            score -= 5
            flags.append("Precio sobre banda de Bollinger superior")
        elif bb_pct < 0:
            score += 3 if above_200 else -3
            flags.append("Precio bajo banda de Bollinger inferior")

        score = float(np.clip(score, 0, 100))
        metrics = {
            "price": price,
            "rsi_14": rsi14,
            "macd": macd_v,
            "macd_signal": macd_sig,
            "macd_hist": macd_hist,
            "sma_50": sma50,
            "sma_200": sma200,
            "price_vs_sma50_pct": (price / sma50 - 1) * 100 if math.isfinite(sma50) else None,
            "price_vs_sma200_pct": (price / sma200 - 1) * 100 if has_sma200 else None,
            "sma200_slope_20d_pct": sma200_slope * 100 if math.isfinite(sma200_slope) else None,
            "bb_lower": bb_lower,
            "bb_mid": bb_mid,
            "bb_upper": bb_upper,
            "bb_percent_b": bb_pct,
            "atr_14": atr14,
            "atr_pct": atr14 / price if price else None,
            "return_20d_pct": ret20 * 100,
        }
        metrics = {k: (round(v, 4) if isinstance(v, float) and math.isfinite(v) else v) for k, v in metrics.items()}
        trend = "alcista" if "Tendencia alcista" in flags else "bajista" if "Tendencia bajista" in flags else "lateral"
        return AgentReport(
            agent=self.name, score=score, scale=self.scale, metrics=metrics, flags=flags,
            summary=f"Tendencia {trend}; RSI {rsi14:.1f}; MACD hist {macd_hist:+.2f}.",
        )
