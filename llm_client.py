"""Cliente LLM (Claude) con salida JSON estricta mediante structured outputs."""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from config import SETTINGS

log = logging.getLogger(__name__)

# Reintento automático en otro modelo si un clasificador de seguridad rechaza la petición.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(RuntimeError):
    """El LLM no pudo responder (sin credenciales, red, rechazo...). Se usa el modo determinista."""


class LLMClient:
    def __init__(self, model: str | None = None, client: anthropic.Anthropic | None = None) -> None:
        self.model = model or SETTINGS.llm_model
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            # Resuelve ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN o el perfil de `ant auth login`.
            self._client = anthropic.Anthropic(max_retries=3)
        return self._client

    def json_completion(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        effort: str = "medium",
        max_tokens: int = 16000,
    ) -> dict[str, Any]:
        """Devuelve un dict que cumple `schema` (garantizado por output_config.format)."""
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as exc:
            raise LLMUnavailable(f"Credenciales de Anthropic no válidas: {exc}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMUnavailable(f"Límite de peticiones alcanzado: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailable(f"Error de la API ({exc.status_code}): {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailable(f"Sin conexión con la API: {exc}") from exc
        except TypeError as exc:  # el SDK no encuentra credenciales
            raise LLMUnavailable(str(exc)) from exc

        if response.stop_reason == "refusal":
            raise LLMUnavailable(f"El modelo rechazó la petición: {response.stop_details}")
        if response.stop_reason == "max_tokens":
            raise LLMUnavailable("Respuesta truncada (max_tokens)")

        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMUnavailable("Respuesta sin bloque de texto")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMUnavailable(f"JSON inválido del LLM: {exc}") from exc
