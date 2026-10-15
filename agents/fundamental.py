"""Agente Fundamental: salud financiera. Python puro sobre datos de yfinance."""
from __future__ import annotations

import math
from typing import Any

import pandas as pd

from agents.base import BaseAgent
from config import DEFAULT_SECTOR_PE, SECTOR_PE
from models import AgentReport, MarketSnapshot

NEUTRAL = 10.0  # puntuación de un sub-factor sin datos (sobre 20)


def _num(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _row(df: pd.DataFrame | None, *names: str) -> pd.Series | None:
    if df is None or getattr(df, "empty", True):
        return None
    for n in names:
        if n in df.index:
            s = pd.to_numeric(df.loc[n], errors="coerce").dropna()
            if not s.empty:
                return s.sort_index(ascending=False)  # más reciente primero
    return None


class FundamentalAgent(BaseAgent):
    name = "fundamental"
    scale = (0.0, 100.0)

    # ------------------------------------------------------------- métricas
    @staticmethod
    def extract_metrics(snapshot: MarketSnapshot) -> dict[str, Any]:
        info = snapshot.info or {}
        sector = info.get("sector") or "Unknown"
        sector_pe = SECTOR_PE.get(sector, DEFAULT_SECTOR_PE)

        pe = _num(info.get("trailingPE")) or _num(info.get("forwardPE"))

        eps_growth = None
        eps = _row(snapshot.income_stmt, "Diluted EPS", "Basic EPS")
        if eps is not None and len(eps) >= 2 and eps.iloc[1] != 0:
            eps_growth = float((eps.iloc[0] - eps.iloc[1]) / abs(eps.iloc[1]))
        if eps_growth is None:
            eps_growth = _num(info.get("earningsGrowth"))

        net_margin = _num(info.get("profitMargins"))
        if net_margin is None:
            ni = _row(snapshot.income_stmt, "Net Income")
            rev = _row(snapshot.income_stmt, "Total Revenue")
            if ni is not None and rev is not None and rev.iloc[0]:
                net_margin = float(ni.iloc[0] / rev.iloc[0])

        de = _num(info.get("debtToEquity"))
        debt_equity = de / 100.0 if de is not None else None  # yfinance lo da en %

        fcf = _num(info.get("freeCashflow"))
        if fcf is None:
            fcf_row = _row(snapshot.cashflow, "Free Cash Flow")
            fcf = float(fcf_row.iloc[0]) if fcf_row is not None else None
        mcap = _num(info.get("marketCap"))
        fcf_yield = fcf / mcap if (fcf is not None and mcap) else None

        return {
            "sector": sector,
            "pe_ratio": pe,
            "sector_pe": sector_pe,
            "pe_vs_sector": pe / sector_pe if pe and pe > 0 else None,
            "eps_growth_yoy": eps_growth,
            "net_margin": net_margin,
            "debt_equity": debt_equity,
            "free_cash_flow": fcf,
            "fcf_yield": fcf_yield,
            "market_cap": mcap,
        }

    # ------------------------------------------------------------- scoring
    @staticmethod
    def _score_pe(rel: float | None, pe: float | None) -> float:
        if pe is not None and pe <= 0:
            return 2.0  # beneficios negativos
        if rel is None:
            return NEUTRAL
        for limit, pts in ((0.7, 20), (0.9, 16), (1.1, 12), (1.4, 8), (2.0, 4)):
            if rel < limit:
                return float(pts)
        return 2.0

    @staticmethod
    def _score_eps(g: float | None) -> float:
        if g is None:
            return NEUTRAL
        for limit, pts in ((0.25, 20), (0.10, 16), (0.0, 12), (-0.10, 6)):
            if g > limit:
                return float(pts)
        return 2.0

    @staticmethod
    def _score_margin(m: float | None) -> float:
        if m is None:
            return NEUTRAL
        for limit, pts in ((0.20, 20), (0.10, 15), (0.05, 10), (0.0, 6)):
            if m > limit:
                return float(pts)
        return 2.0

    @staticmethod
    def _score_de(de: float | None, sector: str) -> float:
        if de is None or sector == "Financial Services":  # apalancamiento estructural en banca
            return NEUTRAL
        if de < 0:
            return 2.0  # patrimonio neto negativo
        for limit, pts in ((0.5, 20), (1.0, 15), (2.0, 9), (3.0, 4)):
            if de < limit:
                return float(pts)
        return 1.0

    @staticmethod
    def _score_fcf(fcf: float | None, fcf_yield: float | None) -> float:
        if fcf is None:
            return NEUTRAL
        if fcf <= 0:
            return 2.0
        if fcf_yield is None:
            return 12.0
        for limit, pts in ((0.05, 20), (0.03, 16), (0.015, 12)):
            if fcf_yield > limit:
                return float(pts)
        return 8.0

    def analyze(self, snapshot: MarketSnapshot) -> AgentReport:
        m = self.extract_metrics(snapshot)
        breakdown = {
            "pe_vs_sector": self._score_pe(m["pe_vs_sector"], m["pe_ratio"]),
            "eps_growth": self._score_eps(m["eps_growth_yoy"]),
            "net_margin": self._score_margin(m["net_margin"]),
            "debt_equity": self._score_de(m["debt_equity"], m["sector"]),
            "free_cash_flow": self._score_fcf(m["free_cash_flow"], m["fcf_yield"]),
        }
        score = sum(breakdown.values())
        missing = [k for k in ("pe_ratio", "eps_growth_yoy", "net_margin", "debt_equity", "free_cash_flow") if m[k] is None]

        flags: list[str] = []
        if len(missing) >= 3:
            flags.append("Datos fundamentales incompletos")
        if m["free_cash_flow"] is not None and m["free_cash_flow"] <= 0:
            flags.append("Free Cash Flow negativo")
        if m["debt_equity"] is not None and m["debt_equity"] > 2 and m["sector"] != "Financial Services":
            flags.append("Apalancamiento elevado")

        m["score_breakdown"] = breakdown
        m["missing_metrics"] = missing
        return AgentReport(
            agent=self.name, score=float(score), scale=self.scale, metrics=m, flags=flags,
            summary=f"Sector {m['sector']}; puntuación fundamental {score:.0f}/100.",
        )
