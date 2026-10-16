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
ACTION_LABEL = {"BUY": "COMPRAR", "SELL": "VENDER", "REDUCE": "REDUCIR", "HOLD": "MANTENER"}
CONFIDENCE_LABEL = {"HIGH": "alta", "MEDIUM": "media", "LOW": "baja"}
LLM_OFFLINE_NOTE = "⚠️ Nota: Análisis redactado en modo matemático (IA de Claude desconectada o sin API key)."
_LLM_OFFLINE_PREFIX = "CIO en modo determinista"


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


def support_from_reports(reports: dict[str, Any]) -> dict[str, Any] | None:
    """Extrae el soporte clave del informe del Agente Técnico (None si no hay)."""
    tech = reports.get("technical")
    m = getattr(tech, "metrics", None) or {}
    if m.get("soporte_clave") is None:
        return None
    return {
        "price": m["soporte_clave"],
        "type": m.get("soporte_clave_tipo") or "",
        "distance_pct": m.get("soporte_clave_distancia_pct"),
        "currency": m.get("divisa") or "",
    }


def format_support(support: dict[str, Any] | None) -> str | None:
    if not support:
        return None
    e = html.escape
    detail = e(support.get("type") or "")
    if support.get("distance_pct") is not None:
        detail += f", {support['distance_pct']:+.1f}%"
    return (
        f"🛡️ Soporte clave cercano: <b>{support['price']:,.2f} {e(support.get('currency') or '')}</b>".rstrip()
        + (f" ({detail})" if detail else "")
    )


def clean_notes(notes: Iterable[str]) -> tuple[list[str], bool]:
    """Quita del mensaje los errores técnicos del LLM. Devuelve (notas, ¿IA desconectada?)."""
    kept, offline = [], False
    for n in notes:
        if n.startswith(_LLM_OFFLINE_PREFIX):
            offline = True
        else:
            kept.append(n)
    return kept, offline


def _n(x: float, decimals: int = 2) -> str:
    """Número en formato español: 1.234,56."""
    return f"{x:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(x: float) -> str:
    return f"{x:+.1f}".replace(".", ",").replace("-", "−") + " %"


def _agent_lines(reports: dict[str, Any]) -> list[str]:
    def get(name):
        return reports.get(name)

    def score(rep, signed=False):
        return f"{rep.score:+.0f}" if signed else f"{rep.score:.0f}/100"

    lines = ["📊 <b>Análisis</b>"]
    f = get("fundamental")
    if f is not None:
        # Mismo umbral que el francotirador usa para "fundamental intacto".
        from config import SETTINGS

        solid = SETTINGS.alerts.dip_min_fundamental
        label = "sin datos" if f.error else "Sólido" if f.score >= solid else "Neutro" if f.score >= 40 else "Débil"
        lines.append(f"🏢 Fundamental: <b>{'—' if f.error else score(f)}</b> ({label})")
    t = get("technical")
    if t is not None:
        trend = (
            "sin datos" if t.error
            else "Alcista" if "Tendencia alcista" in t.flags
            else "Bajista" if "Tendencia bajista" in t.flags
            else "Lateral"
        )
        lines.append(f"📈 Técnico: <b>{'—' if t.error else score(t)}</b> ({trend})")
    s = get("sentiment")
    if s is not None:
        mood = "Positivo" if s.score >= 25 else "Negativo" if s.score <= -25 else "Neutral"
        lines.append(f"📰 Sentimiento: <b>{score(s, signed=True)}</b> ({mood})")
    r = get("risk")
    if r is not None:
        level = (
            "sin datos" if r.error
            else "Volatilidad extrema" if any("Volatilidad extrema" in fl for fl in r.flags)
            else "Riesgo bajo" if r.score >= 65 else "Riesgo moderado" if r.score >= 40 else "Riesgo alto"
        )
        lines.append(f"🛡️ Riesgo: <b>{'—' if r.error else score(r)}</b> ({level})")
    return lines if len(lines) > 1 else []


def format_analysis(result: Any, header: str | None = None, strong: int = 60) -> str:
    """Mensaje completo y legible de un análisis (`main.AnalysisResult`).

    Lo usan /analiza, la alerta diaria y el francotirador. Toma lo que haya en `result`
    (decision obligatoria; reports, plan y snapshot opcionales).
    """
    e = html.escape
    d = result.decision
    reports = getattr(result, "reports", None) or {}
    plan = getattr(result, "plan", None)
    snap = getattr(result, "snapshot", None)
    tech_m = getattr(reports.get("technical"), "metrics", None) or {}
    currency = e(getattr(snap, "currency", None) or tech_m.get("divisa") or "")
    price = getattr(plan, "entry_price", None) or tech_m.get("price")
    action = d.action.value

    lines = [header] if header else []
    lines += [
        f"{ACTION_EMOJI.get(action, 'ℹ️')} <b>{ACTION_LABEL.get(action, action)} · {e(d.ticker)}</b>",
        f"Puntuación: <b>{d.final_score:+d}</b> · {strength_label(d.final_score, strong)} · "
        f"confianza {CONFIDENCE_LABEL.get(d.confidence_level.value, d.confidence_level.value)}",
    ]
    if price:
        lines.append(f"💵 Precio: {_n(price)} {currency}")

    agents = _agent_lines(reports)
    if agents:
        lines += [""] + agents

    # --- Orden: lo importante, destacado
    order = ["", "🎯 <b>Niveles de la operación</b>"]
    if action == "BUY" and d.suggested_position_size_eur:
        order.append(f"💶 Tamaño sugerido: <b>{_n(d.suggested_position_size_eur)} €</b>")

    def level(icon, name, value):
        dist = f" ({_pct((value / price - 1) * 100)})" if price else ""
        return f"{icon} {name}: <b>{_n(value)} {currency}</b>{dist}"

    order.append(level("🛑", "Stop-loss", d.stop_loss_price))
    order.append(level("✅", "Take-profit", d.take_profit_target))
    support = support_from_reports(reports)
    if support:
        dist = f" ({_pct(support['distance_pct'])})" if support.get("distance_pct") is not None else ""
        order.append(f"🛡️ Soporte clave cercano: <b>{_n(support['price'])} {currency}</b> · {e(support['type'])}{dist}")
    lines += order

    if d.vetoes:
        lines += ["", "🚫 <b>Veto de riesgo:</b> " + e("; ".join(v.split(": ", 1)[-1] for v in d.vetoes))]

    notes, offline = clean_notes(d.guardrail_notes)
    if d.justification and not offline:
        lines += ["", f"💬 <i>{e(d.justification)}</i>"]
    if notes:
        lines += [""] + [f"• {e(n)}" for n in notes]
    if offline:
        lines += ["", LLM_OFFLINE_NOTE]
    return "\n".join(lines)


def format_signal(
    signal_json: dict[str, Any],
    vetoes: Iterable[str] = (),
    notes: Iterable[str] = (),
    strong: int = 60,
    support: dict[str, Any] | None = None,
    header: str | None = None,
) -> str:
    """Mensaje HTML a partir del JSON estricto del CIO (`CIODecision.to_output_json()`).

    `support` es el soporte clave del Agente Técnico (ver `support_from_reports`) y `header`
    una cabecera opcional (p. ej. la alerta de oportunidad de caída).
    """
    e = html.escape
    action = str(signal_json.get("action", "HOLD"))
    score = int(signal_json.get("final_score", 0))
    params = signal_json.get("execution_parameters", {})
    lines = [header] if header else []
    lines += [
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
    support_line = format_support(support)
    if support_line:
        lines.append(support_line)
    vetoes = list(vetoes)
    if vetoes:
        lines.append("🚫 <b>Veto de riesgo activo:</b> " + e("; ".join(vetoes)))
    notes, offline = clean_notes(notes)
    if signal_json.get("justification") and not offline:
        lines.append(f"\n<i>{e(str(signal_json['justification']))}</i>")
    if notes:
        lines.append("\n" + "\n".join(f"• {e(n)}" for n in notes))
    if offline:
        lines.append("\n" + LLM_OFFLINE_NOTE)
    return "\n".join(lines)


def send_telegram_alert(
    signal_json: dict[str, Any],
    vetoes: Iterable[str] = (),
    notes: Iterable[str] = (),
    strong: int = 60,
    support: dict[str, Any] | None = None,
) -> bool:
    """Envía la señal del CIO con emoji según la acción (🟢 BUY, 🔴 SELL, ⚠️ REDUCE, ⏸️ HOLD)."""
    return send_telegram_message(format_signal(signal_json, vetoes, notes, strong, support))
