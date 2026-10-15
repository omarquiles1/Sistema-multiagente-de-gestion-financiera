import json

import anthropic
import httpx2
import pytest

from agents.cio import CIO_SCHEMA
from llm_client import FALLBACK_BETA, LLMClient, LLMUnavailable


def _client(handler):
    return anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


def _message(text, stop_reason="end_turn"):
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_request_uses_structured_output_and_fallbacks():
    seen = {}

    def handler(req):
        seen["beta"] = req.headers.get("anthropic-beta")
        seen["body"] = json.loads(req.content)
        return httpx2.Response(200, json=_message('{"action": "HOLD"}'))

    out = LLMClient(client=_client(handler)).json_completion("sys", "user", CIO_SCHEMA, effort="high")
    assert out == {"action": "HOLD"}
    body = seen["body"]
    assert FALLBACK_BETA in seen["beta"]
    assert body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": CIO_SCHEMA}}


def test_refusal_raises_unavailable():
    handler = lambda req: httpx2.Response(200, json=_message("", stop_reason="refusal"))  # noqa: E731
    with pytest.raises(LLMUnavailable):
        LLMClient(client=_client(handler)).json_completion("s", "u", CIO_SCHEMA)


def test_auth_error_raises_unavailable():
    handler = lambda req: httpx2.Response(  # noqa: E731
        401, json={"type": "error", "error": {"type": "authentication_error", "message": "bad key"}}
    )
    with pytest.raises(LLMUnavailable):
        LLMClient(client=_client(handler)).json_completion("s", "u", CIO_SCHEMA)
