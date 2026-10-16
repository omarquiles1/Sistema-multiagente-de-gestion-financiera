"""Orquestador del Sistema Multiagente de Gestión Financiera.

Flujo estricto:
  1. Recibe un ticker.
  2. Descarga datos crudos (OHLCV 2 años, fundamentales, noticias, divisa, tipos FED/BCE).
  3. Ejecuta en paralelo los 4 agentes especialistas (Fundamental, Técnico, Sentimiento, Riesgo).
  4. Recoge sus scores y métricas.
  5. Calcula el plan de riesgo (RiskManager, código duro) sobre el Capital Total
     (liquidez + valor actual de la cartera) y envía todo al Agente CIO.
  6. El CIO emite el JSON final (consenso + guardarraíles).
  7. Guarda el registro en SQLite.

Uso:
  python main.py MSFT
  python main.py MSFT AAPL SAN.MC --db cartera.db
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from agents.base import BaseAgent
from agents.cio import CIOAgent
from agents.fundamental import FundamentalAgent
from agents.risk import RiskAgent
from agents.sentiment import SentimentAgent
from agents.technical import TechnicalAgent
from config import SETTINGS, Settings
from data.market_data import DataFetcher
from database import Database
from llm_client import LLMClient
from models import AgentReport, CIODecision, MarketSnapshot, RiskPlan
from portfolio import PortfolioManager
from risk_manager import RiskManager

log = logging.getLogger("orchestrator")


def apply_live_price(ohlcv, live_price: float):
    """Fija el precio en vivo como cierre de la sesión de hoy (o la añade si aún no existe)."""
    import pandas as pd

    df = ohlcv.copy()
    today = pd.Timestamp.now().normalize()
    if pd.Timestamp(df.index[-1]).normalize() == today:
        i = df.index[-1]
        df.loc[i, "close"] = live_price
        df.loc[i, "high"] = max(df.loc[i, "high"], live_price)
        df.loc[i, "low"] = min(df.loc[i, "low"], live_price)
    else:
        df.loc[today] = {"open": live_price, "high": live_price, "low": live_price, "close": live_price, "volume": 0.0}
    return df


@dataclass
class AnalysisResult:
    snapshot: MarketSnapshot
    reports: dict[str, AgentReport]
    plan: RiskPlan
    decision: CIODecision
    signal_id: int


class Orchestrator:
    def __init__(
        self,
        settings: Settings = SETTINGS,
        fetcher: DataFetcher | None = None,
        db: Database | None = None,
        llm: LLMClient | None = None,
        capital_fn: Callable[[], tuple[float, float] | None] | None = None,
        exposure_fn: Callable[[str], float] | None = None,
    ) -> None:
        self.settings = settings
        self.fetcher = fetcher or DataFetcher(settings)
        self.db = db or Database(settings.db_path)
        # (capital total, liquidez) en EUR; None -> capital base fijo de config.RiskRules.
        # exposure_fn(ticker): € que ya se tienen de ese activo (cuenta para el límite del 10 %).
        portfolio = PortfolioManager(self.db) if capital_fn is None or exposure_fn is None else None
        self.capital_fn = capital_fn or portfolio.capital
        self.exposure_fn = exposure_fn or portfolio.position_value_eur
        llm = llm or LLMClient(settings.llm_model)
        self.specialists: list[BaseAgent] = [
            FundamentalAgent(),
            TechnicalAgent(),
            SentimentAgent(llm),
            RiskAgent(settings.thresholds),
        ]
        self.risk_manager = RiskManager(settings.risk)
        self.cio = CIOAgent(llm, settings)

    def run_specialists(self, snapshot: MarketSnapshot) -> dict[str, AgentReport]:
        with ThreadPoolExecutor(max_workers=len(self.specialists)) as pool:
            futures = {a.name: pool.submit(a.run, snapshot) for a in self.specialists}
            return {name: f.result() for name, f in futures.items()}

    def analyze(self, ticker: str, live_price: float | None = None) -> AnalysisResult:
        """`live_price`: último precio de 1 minuto (francotirador); sustituye al cierre de hoy."""
        ticker = ticker.upper().strip()
        log.info("[1-2] Descargando datos de %s", ticker)
        snapshot = self.fetcher.fetch(ticker)
        if live_price is not None:
            snapshot.ohlcv = apply_live_price(snapshot.ohlcv, live_price)

        log.info("[3-4] Ejecutando agentes especialistas en paralelo")
        reports = self.run_specialists(snapshot)

        log.info("[5] RiskManager + Agente CIO")
        atr14 = reports["technical"].metrics.get("atr_14") or reports["risk"].metrics.get("atr_14")
        total, cash = self._capital()
        plan = self.risk_manager.build_plan(
            snapshot.last_price, atr14, snapshot.fx_to_eur, cash_available_eur=cash, capital_eur=total,
            existing_position_eur=self._exposure(ticker),
        )
        lookback = max(self.settings.thresholds.signal_cooldown_days, self.settings.thresholds.wash_sale_days)
        recent = self.db.recent_signals(ticker, lookback)
        decision = self.cio.decide(ticker, reports, plan, recent)

        log.info("[7] Persistiendo en SQLite")
        signal_id = self._persist(snapshot, reports, plan, decision)
        return AnalysisResult(snapshot, reports, plan, decision, signal_id)

    def _capital(self) -> tuple[float | None, float | None]:
        try:
            capital = self.capital_fn()
        except Exception as exc:
            log.warning("No se pudo valorar la cartera; se usa el capital base: %s", exc)
            capital = None
        return capital if capital else (None, None)

    def _exposure(self, ticker: str) -> float:
        try:
            return float(self.exposure_fn(ticker))
        except Exception as exc:
            log.warning("No se pudo valorar la posición actual de %s: %s", ticker, exc)
            return 0.0

    def _persist(
        self, s: MarketSnapshot, reports: dict[str, AgentReport], plan: RiskPlan, d: CIODecision
    ) -> int:
        self.db.upsert_market_data(s.ticker, s.ohlcv)
        fm = reports["fundamental"].metrics
        self.db.upsert_fundamentals(
            s.ticker, fm.get("pe_ratio"), fm.get("eps_growth_yoy"), fm.get("debt_equity"), fm.get("free_cash_flow")
        )
        justification = d.justification
        if d.guardrail_notes:
            justification += " | Guardarraíles: " + " ; ".join(d.guardrail_notes)
        return self.db.log_signal(
            ticker=s.ticker,
            fundamental_score=reports["fundamental"].score,
            technical_score=reports["technical"].score,
            sentiment_score=reports["sentiment"].score,
            final_score=d.final_score,
            recommended_action=d.action.value,
            confidence_level=d.confidence_level.value,
            stop_loss_calculated=plan.stop_loss_price,
            position_size_calculated=d.suggested_position_size_eur,
            llm_justification=justification,
        )


def _print_report(r: AnalysisResult) -> None:
    s, p = r.snapshot, r.plan
    print(f"\n=== {s.ticker} | {s.last_price:.2f} {s.currency} (1 {s.currency} = {s.fx_to_eur:.4f} €) ===")
    for name, rep in r.reports.items():
        flags = f"  [{', '.join(rep.flags)}]" if rep.flags else ""
        print(f"  {name:<12} score={rep.score:7.1f}  norm={rep.normalized():+7.1f}{flags}")
        if rep.error:
            print(f"  {'':<12} error: {rep.error}")
    print(
        f"  RiskManager  SL={p.stop_loss_price:.2f} (ATR {p.atr_stop:.2f} / duro {p.hard_stop:.2f})  "
        f"TP={p.take_profit_target:.2f}  tamaño={p.position_size_eur:.2f} €  riesgo={p.risk_eur:.2f} €  "
        f"(capital {p.capital_eur:.0f} €)  "
        f"{'OK' if p.tradeable else 'NO OPERABLE: ' + p.reason}"
    )
    for note in r.decision.guardrail_notes:
        print(f"  ! {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sistema Multiagente de Gestión Financiera")
    parser.add_argument("tickers", nargs="+", help="Tickers de Yahoo Finance (MSFT, SAN.MC, ASML.AS...)")
    parser.add_argument("--db", default=SETTINGS.db_path, help="Ruta de la base de datos SQLite")
    parser.add_argument("--json-only", action="store_true", help="Imprime solo el JSON del CIO")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    orchestrator = Orchestrator(db=Database(args.db))
    exit_code = 0
    for ticker in args.tickers:
        try:
            result = orchestrator.analyze(ticker)
        except Exception as exc:
            log.error("No se pudo analizar %s: %s", ticker, exc)
            exit_code = 1
            continue
        if not args.json_only:
            _print_report(result)
        print(json.dumps(result.decision.to_output_json(), ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
