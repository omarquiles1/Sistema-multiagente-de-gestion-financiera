"""Watchlist predefinida: activos que el servicio continuo analiza y empresas que vigila en noticias."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from config import SETTINGS


@dataclass(frozen=True)
class CorporateTarget:
    """Empresa (cotizada o no) cuyas noticias se rastrean en busca de eventos relevantes."""

    name: str
    queries: tuple[str, ...]
    keywords: dict[str, tuple[str, ...]]       # categoría -> palabras clave (minúsculas)
    must_mention: tuple[str, ...] = ()         # el titular debe contener alguna (filtra homónimos)


@dataclass(frozen=True)
class Watchlist:
    groups: dict[str, tuple[str, ...]]
    labels: dict[str, str] = field(default_factory=dict)
    corporate_watch: tuple[CorporateTarget, ...] = ()

    @property
    def tickers(self) -> list[str]:
        """Todos los tickers, sin duplicados y en el orden del fichero."""
        return list(dict.fromkeys(t for group in self.groups.values() for t in group))


def load_watchlist(path: str | Path | None = None) -> Watchlist:
    raw = json.loads(Path(path or SETTINGS.watchlist_path).read_text(encoding="utf-8"))
    groups = {k: tuple(t.upper().strip() for t in g["tickers"]) for k, g in raw.get("groups", {}).items()}
    labels = {k: g.get("label", k) for k, g in raw.get("groups", {}).items()}
    targets = tuple(
        CorporateTarget(
            name=c["name"],
            queries=tuple(c.get("queries") or [c["name"]]),
            keywords={cat: tuple(w.lower() for w in words) for cat, words in c.get("keywords", {}).items()},
            must_mention=tuple(w.lower() for w in c.get("must_mention", [])),
        )
        for c in raw.get("corporate_watch", [])
    )
    return Watchlist(groups=groups, labels=labels, corporate_watch=targets)
