"""Bot interactivo de Telegram: gestiona la cartera y lanza análisis a demanda.

Comandos:
  /cartera                              valor actual, P&L por activo y total, liquidez
  /comprar TICKER CANTIDAD [PRECIO]     registra una compra (precio en la divisa del activo;
                                        sin precio, se usa la cotización actual)
  /vender TICKER CANTIDAD [PRECIO]      registra una venta
  /analiza TICKER                       ejecuta los 5 agentes ahora mismo
  /operaciones                          últimas operaciones registradas
  /ayuda                                esta ayuda

Funciona por *long polling* (getUpdates) con `requests` en un hilo, así convive con el bucle
síncrono de `schedule` sin bucle de eventos asíncrono. Seguridad:
  * Solo se atienden mensajes del chat TELEGRAM_CHAT_ID; el resto se ignora.
  * Los comandos con más de 15 min de antigüedad (enviados con el bot apagado) no se ejecutan.

Uso independiente (sin el planificador):
  python telegram_listener.py
"""
from __future__ import annotations

import html
import logging
import os
import sys
import threading
import time
from typing import Any, Callable

import requests

from config import SETTINGS
from notifier import format_signal, send_telegram_message, support_from_reports
from portfolio import PortfolioError, PortfolioManager, format_portfolio, format_trade

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"

COMMANDS = [
    ("cartera", "Valor actual y P&L de la cartera"),
    ("comprar", "TICKER CANTIDAD [PRECIO] · registra una compra"),
    ("vender", "TICKER CANTIDAD [PRECIO] · registra una venta"),
    ("analiza", "TICKER · ejecuta los 5 agentes ahora"),
    ("operaciones", "Últimas operaciones registradas"),
    ("ayuda", "Lista de comandos"),
]

HELP = (
    "🤖 <b>Comandos</b>\n"
    "/cartera — valor actual y P&amp;L\n"
    "/comprar TICKER CANTIDAD [PRECIO]\n"
    "/vender TICKER CANTIDAD [PRECIO]\n"
    "/analiza TICKER — análisis completo ahora\n"
    "/operaciones — últimas operaciones\n\n"
    "Precio en la divisa del activo (USD para PYPL u O). Sin precio se usa la cotización actual.\n"
    "Decimales con coma o punto: <code>/comprar SPPW.DE 2,5 48,10</code>"
)


def _num(text: str) -> float:
    try:
        return float(text.replace(",", "."))
    except ValueError:
        raise PortfolioError(f"«{text}» no es un número.") from None


class TelegramBot:
    def __init__(
        self,
        portfolio: PortfolioManager,
        analyze_fn: Callable[[str], Any],
        token: str | None = None,
        chat_id: str | None = None,
        send: Callable[[str], bool] = send_telegram_message,
        max_age_s: int = 900,
        poll_timeout_s: int = 25,
    ) -> None:
        self.portfolio = portfolio
        self.analyze_fn = analyze_fn
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = str(chat_id or os.getenv("TELEGRAM_CHAT_ID", ""))
        self.send = send
        self.max_age_s = max_age_s
        self.poll_timeout_s = poll_timeout_s
        self.offset: int | None = None
        self.handlers: dict[str, Callable[[list[str]], str | None]] = {
            "cartera": self.cmd_cartera,
            "comprar": self.cmd_comprar,
            "vender": self.cmd_vender,
            "analiza": self.cmd_analiza,
            "operaciones": self.cmd_operaciones,
            "ayuda": lambda args: HELP,
            "start": lambda args: HELP,
        }

    # ------------------------------------------------------------ comandos
    def cmd_cartera(self, args: list[str]) -> str:
        rules = SETTINGS.risk
        return format_portfolio(self.portfolio.snapshot(), rules.max_position_pct, rules.max_risk_pct)

    def _trade_args(self, args: list[str], verb: str) -> tuple[str, float, float | None]:
        if len(args) not in (2, 3):
            raise PortfolioError(f"Uso: /{verb} TICKER CANTIDAD [PRECIO]")
        return args[0].upper(), _num(args[1]), _num(args[2]) if len(args) == 3 else None

    def cmd_comprar(self, args: list[str]) -> str:
        return format_trade(self.portfolio.buy(*self._trade_args(args, "comprar")))

    def cmd_vender(self, args: list[str]) -> str:
        return format_trade(self.portfolio.sell(*self._trade_args(args, "vender")))

    def cmd_analiza(self, args: list[str]) -> str:
        if len(args) != 1:
            raise PortfolioError("Uso: /analiza TICKER")
        ticker = args[0].upper()
        self.send(f"⏳ Analizando {html.escape(ticker)}… (puede tardar un minuto)")
        try:
            result = self.analyze_fn(ticker)
        except Exception as exc:
            return f"❌ No se pudo analizar {html.escape(ticker)}: {html.escape(str(exc))}"
        d, p = result.decision, result.plan
        scores = " · ".join(
            f"{name[:4].capitalize()} {rep.normalized():+.0f}" for name, rep in result.reports.items()
        )
        sizing = (
            f"Plan: {p.position_size_eur:.2f} € (máx. {p.max_position_eur:.0f} € sobre {p.capital_eur:.0f} € de capital)"
            if p.tradeable else f"Plan: no operable · {html.escape(p.reason)}"
        )
        return (
            format_signal(d.to_output_json(), d.vetoes, d.guardrail_notes, support=support_from_reports(result.reports))
            + f"\n\n📊 Agentes (−100..100): {scores}\n{sizing}"
        )

    def cmd_operaciones(self, args: list[str]) -> str:
        trades = self.portfolio.db.trades(10)
        if not trades:
            return "Sin operaciones registradas."
        lines = ["🧾 <b>Últimas operaciones</b>"]
        for t in trades:
            pnl = f" · P&amp;L {t['pnl_realizado_eur']:+.2f} €" if t["pnl_realizado_eur"] is not None else ""
            lines.append(
                f"{t['timestamp'][:16].replace('T', ' ')} {t['side']} {html.escape(t['ticker'])} "
                f"{t['cantidad']:g} × {t['precio']:,.2f} {t['divisa']}{pnl}"
            )
        return "\n".join(lines)

    # ------------------------------------------------------------ despacho
    def handle_text(self, text: str) -> str | None:
        parts = text.strip().split()
        if not parts or not parts[0].startswith("/"):
            return HELP
        command = parts[0][1:].split("@", 1)[0].lower()   # /cartera@OmarFinanzas_bot
        handler = self.handlers.get(command)
        if handler is None:
            return f"Comando desconocido: /{html.escape(command)}\n\n{HELP}"
        try:
            return handler(parts[1:])
        except PortfolioError as exc:
            return f"⚠️ {html.escape(str(exc))}"
        except Exception as exc:
            log.exception("Fallo en /%s", command)
            return f"❌ Error interno en /{command}: {html.escape(type(exc).__name__)}"

    def process_update(self, update: dict[str, Any], now: float | None = None) -> str | None:
        msg = update.get("message") or update.get("edited_message")
        if not msg or "text" not in msg:
            return None
        if str(msg.get("chat", {}).get("id")) != self.chat_id:
            log.warning("Mensaje ignorado de un chat no autorizado (%s)", msg.get("chat", {}).get("id"))
            return None
        age = (now or time.time()) - msg.get("date", 0)
        text = msg["text"]
        if age > self.max_age_s:
            reply = (
                f"⏭️ Ignoro «{html.escape(text[:60])}»: se envió hace {age / 60:.0f} min, con el bot "
                "apagado. Repítelo si sigue vigente."
            )
        else:
            reply = self.handle_text(text)
        if reply:
            self.send(reply)
        return reply

    # ------------------------------------------------------------ red
    def _call(self, method: str, http_timeout: float, **params: Any) -> Any:
        r = requests.post(API.format(token=self.token, method=method), json=params, timeout=http_timeout)
        if r.status_code != 200:
            raise RuntimeError(f"Telegram {method} respondió {r.status_code}: {r.text[:200]}")
        return r.json().get("result")

    def register_commands(self) -> None:
        try:
            self._call("setMyCommands", 10, commands=[{"command": c, "description": d} for c, d in COMMANDS])
        except Exception as exc:
            log.warning("No se pudo registrar el menú de comandos: %s", exc)

    def poll_once(self) -> int:
        params: dict[str, Any] = {"timeout": self.poll_timeout_s, "allowed_updates": ["message", "edited_message"]}
        if self.offset is not None:
            params["offset"] = self.offset
        updates = self._call("getUpdates", self.poll_timeout_s + 10, **params) or []
        for u in updates:
            self.offset = u["update_id"] + 1     # confirmado aunque falle: no se repite en bucle
            try:
                self.process_update(u)
            except Exception:
                log.exception("Fallo procesando el mensaje %s", u.get("update_id"))
        return len(updates)

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        if not (self.token and self.chat_id):
            log.error("Listener de Telegram desactivado: faltan TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
            return
        self.register_commands()
        log.warning("Listener de Telegram escuchando comandos")
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                # Sin token en el log: el mensaje de requests no incluye la URL salvo en errores HTTP.
                log.warning("Fallo en getUpdates (%s); reintento en 10 s", type(exc).__name__)
                stop.wait(10)

    def start_in_thread(self, stop: threading.Event | None = None) -> threading.Thread:
        t = threading.Thread(target=self.run, args=(stop,), name="telegram-listener", daemon=True)
        t.start()
        return t


def main() -> int:
    from database import Database
    from main import Orchestrator

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    db = Database(SETTINGS.db_path)
    portfolio = PortfolioManager(db)
    orchestrator = Orchestrator(db=db, capital_fn=portfolio.capital, exposure_fn=portfolio.position_value_eur)
    try:
        TelegramBot(portfolio, orchestrator.analyze).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
