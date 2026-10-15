"""Orquestador del Sistema Multiagente de Gestión Financiera.

Flujo estricto:
  1. Recibe un ticker.
  2. Descarga datos crudos (OHLCV 2 años, fundamentales, noticias, divisa, tipos FED/BCE).
  3. Ejecuta en paralelo los 4 agentes especialistas (Fundamental, Técnico, Sentimiento, Riesgo).
  4. Recoge sus scores y métricas.
  5. Calcula el plan de riesgo (RiskManager, código duro) y envía todo al Agente CIO.
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
from risk_manager import RiskManager

log = logging.getLogger("orchestrator")


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
    ) -> None:
        self.settings = settings
        self.fetcher = fetcher or DataFetcher(settings)
        self.db = db or Database(settings.db_path)
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

    def analyze(self, ticker: str) -> AnalysisResult:
        ticker = ticker.upper().strip()
        log.info("[1-2] Descargando datos de %s", ticker)
        snapshot = self.fetcher.fetch(ticker)

        log.info("[3-4] Ejecutando agentes especialistas en paralelo")
        reports = self.run_specialists(snapshot)

        log.info("[5] RiskManager + Agente CIO")
        atr14 = reports["technical"].metrics.get("atr_14") or reports["risk"].metrics.get("atr_14")
        plan = self.risk_manager.build_plan(snapshot.last_price, atr14, snapshot.fx_to_eur)
        lookback = max(self.settings.thresholds.signal_cooldown_days, self.settings.thresholds.wash_sale_days)
        recent = self.db.recent_signals(ticker, lookback)
        decision = self.cio.decide(ticker, reports, plan, recent)

        log.info("[7] Persistiendo en SQLite")
        signal_id = self._persist(snapshot, reports, plan, decision)
        return AnalysisResult(snapshot, reports, plan, decision, signal_id)

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
        f"{'OK' if p.tradeable else 'NO OPERABLE: ' + p.reason}"
    )
    for note in r.decision.guardrail_notes:
        print(f"  ! {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sistema Multiagente de Gestión Financiera (cartera 7.000 €)")
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
