"""Agente de Riesgo: riesgo de mercado del activo. Python puro, SIN LLM.

Convención: `risk_score` alto (100) = riesgo BAJO (favorable para comprar),
para que pueda ponderarse en positivo dentro del consenso del CIO.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from agents.base import BaseAgent
from agents.technical import atr as atr_indicator
from config import Thresholds
from models import AgentReport, MarketSnapshot

EXTREME_VOL_FLAG = "Volatilidad extrema"
TRADING_DAYS = 252


class RiskAgent(BaseAgent):
    name = "risk"
    scale = (0.0, 100.0)

    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.t = thresholds or Thresholds()

    def analyze(self, snapshot: MarketSnapshot) -> AgentReport:
        df = snapshot.ohlcv
        close = df["close"]
        rets = np.log(close).diff().dropna()
        if len(rets) < 30:
            raise ValueError("Histórico insuficiente para métricas de riesgo")

        vol_60 = float(rets.tail(60).std() * math.sqrt(TRADING_DAYS))
        vol_1y = float(rets.tail(TRADING_DAYS).std() * math.sqrt(TRADING_DAYS))
        last_year = close.tail(TRADING_DAYS)
        max_dd = float((last_year / last_year.cummax() - 1).min())
        var_95 = float(np.percentile(rets.tail(TRADING_DAYS), 5))       # VaR histórico diario
        atr14 = float(atr_indicator(df["high"], df["low"], close, 14).dropna().iloc[-1])
        atr_pct = atr14 / float(close.iloc[-1])
        beta = self._beta(rets, snapshot.benchmark_close)

        score = 100.0
        for limit, pen in ((0.20, 0), (0.30, 10), (0.40, 20), (self.t.extreme_volatility_annual, 35)):
            if vol_60 < limit:
                score -= pen
                break
        else:
            score -= 55
        for limit, pen in ((-0.15, 0), (-0.25, 10), (-0.40, 20)):
            if max_dd > limit:
                score -= pen
                break
        else:
            score -= 30
        if beta is not None:
            score -= 10 if beta > 1.5 else 5 if beta > 1.2 else 0
        if atr_pct >= self.t.extreme_atr_pct:
            score -= 15
        elif atr_pct >= 0.04:
            score -= 8

        flags: list[str] = []
        if vol_60 >= self.t.extreme_volatility_annual or atr_pct >= self.t.extreme_atr_pct:
            flags.append(EXTREME_VOL_FLAG)
        if max_dd <= -0.40:
            flags.append("Drawdown severo (>40 %)")
        if beta is not None and beta > 1.5:
            flags.append("Beta alta")

        score = float(np.clip(score, 0, 100))
        metrics = {
            "volatility_60d_annual": round(vol_60, 4),
            "volatility_1y_annual": round(vol_1y, 4),
            "max_drawdown_1y": round(max_dd, 4),
            "var_95_daily": round(var_95, 4),
            "atr_14": round(atr14, 4),
            "atr_pct": round(atr_pct, 4),
            "beta_vs_sp500": round(beta, 3) if beta is not None else None,
        }
        return AgentReport(
            agent=self.name, score=score, scale=self.scale, metrics=metrics, flags=flags,
            summary=f"Vol. 60d {vol_60:.0%}, drawdown 1a {max_dd:.0%}, ATR {atr_pct:.1%} del precio.",
        )

    @staticmethod
    def _beta(rets: pd.Series, bench_close: pd.Series | None) -> float | None:
        if bench_close is None or len(bench_close) < 30:
            return None
        b = np.log(bench_close).diff().dropna()
        joined = pd.concat([rets, b], axis=1, join="inner").dropna().tail(TRADING_DAYS)
        if len(joined) < 30 or joined.iloc[:, 1].var() == 0:
            return None
        cov = np.cov(joined.iloc[:, 0], joined.iloc[:, 1])
        return float(cov[0, 1] / cov[1, 1])
