"""Notificaciones por Telegram (Bot API vía `requests`).

Variables de entorno (se cargan del `.env` en `config.py`):
  TELEGRAM_BOT_TOKEN  token que da @BotFather
  TELEGRAM_CHAT_ID    chat al que se envían las alertas

Si faltan, las funciones registran un aviso y devuelven False: el servicio nunca se cae por
no poder notificar.
"""
from __future__ import annotations

import html
import logging
import os
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_CHARS = 4096
ACTION_EMOJI = {"BUY": "🟢", "SELL": "🔴", "REDUCE": "⚠️", "HOLD": "⏸️"}


def telegram_configured() -> bool:
    return bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID"))


def send_telegram_message(text: str, timeout: float = 10.0) -> bool:
    """Envía `text` (HTML de Telegram ya escapado). Devuelve True si Telegram lo aceptó."""
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        log.warning("Telegram sin configurar (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID): mensaje no enviado")
        return False
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    try:
        r = requests.post(
            TELEGRAM_API.format(token=token),
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=timeout,
        )
        if r.status_code != 200:
            # No se registra la URL: lleva el token dentro.
            log.error("Telegram respondió %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except requests.RequestException as exc:
        log.error("No se pudo contactar con Telegram: %s", type(exc).__name__)
        return False


def strength_label(final_score: int, strong: int = 60) -> str:
    if final_score > strong:
        return "Fuerte Compra"
    if final_score < -strong:
        return "Fuerte Venta"
    return "Señal moderada"


def format_signal(
    signal_json: dict[str, Any],
    vetoes: Iterable[str] = (),
    notes: Iterable[str] = (),
    strong: int = 60,
) -> str:
    """Mensaje HTML a partir del JSON estricto del CIO (`CIODecision.to_output_json()`)."""
    e = html.escape
    action = str(signal_json.get("action", "HOLD"))
    score = int(signal_json.get("final_score", 0))
    params = signal_json.get("execution_parameters", {})
    lines = [
        f"{ACTION_EMOJI.get(action, 'ℹ️')} <b>{e(action)} · {e(str(signal_json.get('ticker', '?')))}</b>",
        f"Score final: <b>{score:+d}</b> ({strength_label(score, strong)}) · "
        f"Confianza: {e(str(signal_json.get('confidence_level', '?')))}",
    ]
    if action == "BUY" and params.get("suggested_position_size_eur"):
        lines.append(f"💶 Tamaño: {params['suggested_position_size_eur']:.2f} €")
    if params:
        lines.append(
            f"🛑 Stop-loss: {params.get('stop_loss_price', 0):.2f} · "
            f"🎯 Take-profit: {params.get('take_profit_target', 0):.2f}"
        )
    vetoes = list(vetoes)
    if vetoes:
        lines.append("🚫 <b>Veto de riesgo activo:</b> " + e("; ".join(vetoes)))
    if signal_json.get("justification"):
        lines.append(f"\n<i>{e(str(signal_json['justification']))}</i>")
    notes = list(notes)
    if notes:
        lines.append("\n" + "\n".join(f"• {e(n)}" for n in notes))
    return "\n".join(lines)


def send_telegram_alert(
    signal_json: dict[str, Any],
    vetoes: Iterable[str] = (),
    notes: Iterable[str] = (),
    strong: int = 60,
) -> bool:
    """Envía la señal del CIO con emoji según la acción (🟢 BUY, 🔴 SELL, ⚠️ REDUCE, ⏸️ HOLD)."""
    return send_telegram_message(format_signal(signal_json, vetoes, notes, strong))
