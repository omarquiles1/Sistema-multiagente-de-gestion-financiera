"""Agente de vigilancia corporativa: rastrea noticias de empresas concretas (p. ej. Grupo TSK).

Las noticias de yfinance son casi todas en inglés y no cubren bien empresas medianas
españolas (Grupo TSK cotiza como TSK.MC, pero apenas sale ahí): se consulta el RSS de Google
News en español y se aplica una regla determinista de palabras clave por categoría (IPO /
colocación, contrato de gran escala, operación corporativa, litigio). Cada noticia se
notifica una sola vez gracias a la tabla `corporate_news_seen`.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote_plus

import requests

from database import Database
from watchlist import CorporateTarget

log = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=es&gl=ES&ceid=ES:es"


@dataclass
class NewsItem:
    title: str
    link: str
    published: datetime | None = None
    source: str = ""


@dataclass
class CorporateAlert:
    company: str
    item: NewsItem
    categories: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)


def fetch_google_news(query: str, timeout: float = 10.0) -> list[NewsItem]:
    r = requests.get(GOOGLE_NEWS_RSS.format(q=quote_plus(query)), timeout=timeout)
    r.raise_for_status()
    items = []
    for node in ET.fromstring(r.content).iter("item"):
        pub = node.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            published = None
        items.append(NewsItem(
            title=(node.findtext("title") or "").strip(),
            link=(node.findtext("link") or "").strip(),
            published=published,
            source=(node.findtext("source") or "").strip(),
        ))
    return items


def _contains(text: str, phrase: str) -> bool:
    """Coincidencia por palabra/frase completa ('ops' no casa con 'tops', ni 'ipo' con 'tipo')."""
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) is not None


def classify(title: str, target: CorporateTarget) -> tuple[list[str], list[str]]:
    """Devuelve (categorías, palabras clave encontradas). Vacío si no es relevante."""
    text = title.lower()
    if target.must_mention and not any(_contains(text, m) for m in target.must_mention):
        return [], []
    categories, matched = [], []
    for category, words in target.keywords.items():
        hits = [w for w in words if _contains(text, w)]
        if hits:
            categories.append(category)
            matched.extend(hits)
    return categories, matched


class CorporateWatchAgent:
    name = "corporate_watch"

    def __init__(
        self,
        db: Database,
        fetch: Callable[[str], list[NewsItem]] = fetch_google_news,
        max_age_days: int = 14,
    ) -> None:
        self.db = db
        self.fetch = fetch
        self.max_age_days = max_age_days

    def scan(self, target: CorporateTarget, now: datetime | None = None) -> list[CorporateAlert]:
        """Noticias NUEVAS y relevantes de `target`. Las ya vistas no se vuelven a devolver."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=self.max_age_days)
        seen_links: set[str] = set()
        alerts: list[CorporateAlert] = []
        for query in target.queries:
            try:
                items = self.fetch(query)
            except Exception as exc:  # la fuente externa puede fallar; no se cae el servicio
                log.warning("Vigilancia de %s: fallo con la consulta %r: %s", target.name, query, exc)
                continue
            for item in items:
                key = item.link or item.title
                if not key or key in seen_links:
                    continue
                seen_links.add(key)
                if item.published and item.published < cutoff:
                    continue
                categories, matched = classify(item.title, target)
                if categories and self.db.mark_news_seen(target.name, key, item.title):
                    alerts.append(CorporateAlert(target.name, item, categories, matched))
        return alerts


def format_corporate_alert(alert: CorporateAlert) -> str:
    e = html.escape
    when = alert.item.published.strftime("%d/%m/%Y") if alert.item.published else "fecha N/D"
    source = f" · {e(alert.item.source)}" if alert.item.source else ""
    link = f'\n<a href="{e(alert.item.link, quote=True)}">Abrir noticia</a>' if alert.item.link else ""
    return (
        f"🏢 <b>Vigilancia corporativa · {e(alert.company)}</b>\n"
        f"🏷️ {e(', '.join(alert.categories))}\n"
        f"📰 {e(alert.item.title)}\n"
        f"🗓️ {when}{source}{link}"
    )
