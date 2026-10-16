"""Servicio continuo: analiza la watchlist cada día hábil y avisa por Telegram.

En paralelo, un hilo atiende los comandos de Telegram (/cartera, /comprar, /vender,
/analiza): ver `telegram_listener.py`.

Calendario (zona Europe/Madrid, configurable en `config.AlertRules`):
  * Lunes a viernes 22:30 -> análisis de toda la watchlist (+ vigilancia de noticias, si la hay).
  * Viernes 23:00         -> auditoría de acierto de las señales de hace 7 y 30 días.
  * Cada 5 minutos        -> francotirador: caídas intradía con velas de 1 minuto (`sniper.py`),
                             solo para los activos con su mercado abierto.

Solo se envía alerta de una señal si |final_score| > 60 (Fuerte Compra / Fuerte Venta)
o si hay un veto de riesgo activo. Todas las señales se guardan igualmente en SQLite.

Uso:
  python scheduler.py                 # servicio continuo + bot de comandos (Ctrl+C para parar)
  python scheduler.py --no-listener   # servicio continuo sin atender comandos
  python scheduler.py --once          # una pasada ahora mismo y sale
  python scheduler.py --once --dry-run   # sin enviar nada: imprime los mensajes
  python scheduler.py --report-now    # auditoría semanal ahora mismo
  python scheduler.py --sniper-now    # un escaneo del francotirador ahora mismo
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

import schedule

from agents.corporate_watch import CorporateWatchAgent, format_corporate_alert
from config import SETTINGS, Settings
from database import Database
from main import AnalysisResult, Orchestrator
from models import CIODecision
from notifier import format_signal, send_telegram_message, support_from_reports, telegram_configured
from performance_tracker import PerformanceTracker, format_report
from portfolio import PortfolioManager
from sniper import Sniper
from telegram_listener import TelegramBot
from watchlist import Watchlist, load_watchlist

log = logging.getLogger("scheduler")

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")


def should_alert(decision: CIODecision, strong: int = SETTINGS.alerts.strong_signal_score) -> bool:
    """Filtro de alertas: Fuerte Compra (> +strong), Fuerte Venta (< -strong) o veto de riesgo."""
    return decision.final_score > strong or decision.final_score < -strong or bool(decision.vetoes)


@dataclass
class RunSummary:
    analyzed: list[str] = field(default_factory=list)
    alerted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    corporate_alerts: int = 0
    messages: list[str] = field(default_factory=list)


class MarketService:
    def __init__(
        self,
        settings: Settings = SETTINGS,
        watchlist: Watchlist | None = None,
        db: Database | None = None,
        orchestrator: Orchestrator | None = None,
        corporate: CorporateWatchAgent | None = None,
        send: Callable[[str], bool] = send_telegram_message,
        dry_run: bool = False,
        portfolio: PortfolioManager | None = None,
    ) -> None:
        self.settings = settings
        self.watchlist = watchlist or load_watchlist(settings.watchlist_path)
        self.db = db or Database(settings.db_path)
        self.portfolio = portfolio or PortfolioManager(self.db)
        self.orchestrator = orchestrator or Orchestrator(
            settings, db=self.db, capital_fn=self.portfolio.capital, exposure_fn=self.portfolio.position_value_eur
        )
        self.corporate = corporate or CorporateWatchAgent(self.db)
        self.dry_run = dry_run
        self._send = send
        # Un solo análisis a la vez: la pasada diaria y /analiza comparten orquestador y yfinance.
        self._analysis_lock = threading.Lock()
        self.sniper = Sniper(self.analyze, self.emit, rules=settings.alerts)

    def analyze(self, ticker: str, live_price: float | None = None):
        with self._analysis_lock:
            if live_price is None:
                return self.orchestrator.analyze(ticker)
            return self.orchestrator.analyze(ticker, live_price=live_price)

    def emit(self, text: str) -> None:
        if self.dry_run:
            print(text, end="\n\n")
        else:
            self._send(text)

    def notify(self, text: str, summary: RunSummary) -> None:
        summary.messages.append(text)
        self.emit(text)

    # ----------------------------------------------------------------- jobs
    def daily_job(self) -> RunSummary:
        summary = RunSummary()
        strong = self.settings.alerts.strong_signal_score
        for ticker in self.watchlist.tickers:
            try:
                result: AnalysisResult = self.analyze(ticker)
            except Exception as exc:
                log.error("No se pudo analizar %s: %s", ticker, exc)
                summary.failed.append(ticker)
                continue
            summary.analyzed.append(ticker)
            d = result.decision
            log.info("%s -> %s (%+d)", ticker, d.action.value, d.final_score)
            if should_alert(d, strong):
                self.notify(
                    format_signal(
                        d.to_output_json(), d.vetoes, d.guardrail_notes, strong,
                        support=support_from_reports(getattr(result, "reports", {})),
                    ),
                    summary,
                )
                summary.alerted.append(ticker)

        for target in self.watchlist.corporate_watch:
            for alert in self.corporate.scan(target):
                self.notify(format_corporate_alert(alert), summary)
                summary.corporate_alerts += 1

        if summary.failed:
            self.notify(
                "⚙️ Ejecución diaria con incidencias: no se pudo analizar " + ", ".join(summary.failed), summary
            )
        log.info(
            "Pasada diaria: %d analizados, %d alertas, %d fallos, %d noticias corporativas",
            len(summary.analyzed), len(summary.alerted), len(summary.failed), summary.corporate_alerts,
        )
        return summary

    def sniper_job(self):
        return self.sniper.scan(self.watchlist.tickers)

    def weekly_report_job(self, tracker: PerformanceTracker | None = None) -> RunSummary:
        summary = RunSummary()
        self.notify(format_report((tracker or PerformanceTracker(self.db)).evaluate()), summary)
        return summary

    # ------------------------------------------------------------ schedule
    def register(self, scheduler: schedule.Scheduler) -> None:
        a = self.settings.alerts
        for day in WEEKDAYS:
            getattr(scheduler.every(), day).at(a.daily_run_time, a.timezone).do(self._safe, self.daily_job)
        getattr(scheduler.every(), a.weekly_report_day).at(a.weekly_report_time, a.timezone).do(
            self._safe, self.weekly_report_job
        )
        scheduler.every(a.sniper_interval_minutes).minutes.do(self._safe, self.sniper_job)

    @staticmethod
    def _safe(job: Callable[[], object]) -> None:
        # Una excepción no debe matar el bucle: `schedule` la propagaría desde run_pending().
        try:
            job()
        except Exception:
            log.exception("El trabajo %s falló", getattr(job, "__name__", job))

    def start_listener(self, stop: threading.Event | None = None) -> threading.Thread | None:
        if not telegram_configured():
            log.warning("Sin credenciales de Telegram: el bot de comandos no arranca")
            return None
        return TelegramBot(self.portfolio, self.analyze, send=self._send).start_in_thread(stop)

    def run_forever(self, poll_seconds: int = 30, listener: bool = True) -> None:
        scheduler = schedule.Scheduler()
        self.register(scheduler)
        stop = threading.Event()
        if listener:
            self.start_listener(stop)
        log.warning("Servicio iniciado. Próxima ejecución: %s", scheduler.next_run)
        try:
            while True:
                scheduler.run_pending()
                time.sleep(poll_seconds)
        finally:
            stop.set()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Servicio continuo con alertas por Telegram")
    parser.add_argument("--db", default=SETTINGS.db_path)
    parser.add_argument("--watchlist", default=SETTINGS.watchlist_path)
    parser.add_argument("--once", action="store_true", help="Ejecuta la pasada diaria ahora y sale")
    parser.add_argument("--report-now", action="store_true", help="Ejecuta la auditoría semanal ahora y sale")
    parser.add_argument("--dry-run", action="store_true", help="Imprime los mensajes en vez de enviarlos")
    parser.add_argument("--no-listener", action="store_true", help="No atiende comandos de Telegram")
    parser.add_argument("--sniper-now", action="store_true", help="Ejecuta un escaneo del francotirador y sale")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):  # emojis en la consola de Windows
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.dry_run and not telegram_configured():
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID sin definir: las alertas no se enviarán")

    service = MarketService(
        watchlist=load_watchlist(args.watchlist), db=Database(args.db), dry_run=args.dry_run
    )
    if args.once or args.report_now or args.sniper_now:
        if args.sniper_now:
            events = service.sniper_job()
            print(f"Francotirador: {len(events)} disparos")
        if args.once:
            service.daily_job()
        if args.report_now:
            service.weekly_report_job()
        return 0
    try:
        service.run_forever(listener=not args.no_listener and not args.dry_run)
    except KeyboardInterrupt:
        log.warning("Servicio detenido")
    return 0


if __name__ == "__main__":
    sys.exit(main())
