"""Clase base de los agentes especialistas."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from models import AgentReport, MarketSnapshot

log = logging.getLogger(__name__)


class BaseAgent(ABC):
    name: str = "base"
    scale: tuple[float, float] = (0.0, 100.0)

    @abstractmethod
    def analyze(self, snapshot: MarketSnapshot) -> AgentReport:
        """Calcula el informe del agente a partir de los datos crudos."""

    def run(self, snapshot: MarketSnapshot) -> AgentReport:
        """Ejecuta el agente sin propagar excepciones: un fallo produce un informe neutro."""
        try:
            return self.analyze(snapshot)
        except Exception as exc:
            log.exception("Agente %s falló", self.name)
            lo, hi = self.scale
            return AgentReport(
                agent=self.name, score=(lo + hi) / 2, scale=self.scale,
                flags=["Datos insuficientes"], error=f"{type(exc).__name__}: {exc}",
                summary="Análisis no disponible; se asume puntuación neutra.",
            )
