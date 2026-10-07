import json
from pathlib import Path

import anthropic
import httpx2 as httpx
import pytest

from gita_studio.config import load_config
from gita_studio.llm import Claude, ClaudeRefused
from gita_studio.models import Review

ROOT = Path(__file__).resolve().parents[1]
REVIEW = {"fidelity_score": 9, "respect_score": 9, "quote_is_exact": True, "issues": [], "verdict": "approve"}


def make_claude(reply):
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}, **reply})

    client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return Claude(load_config(ROOT / "config.yaml"), client), seen


def test_request_shape():
    claude, seen = make_claude({"content": [{"type": "text", "text": json.dumps(REVIEW)}], "stop_reason": "end_turn"})
    assert claude.structured("review", "sys", "hi", Review).verdict == "approve"
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in seen["beta"]
    assert body["output_config"]["effort"] == "high"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in body and "temperature" not in body


def test_refusal_raises():
    claude, _ = make_claude({"content": [], "stop_reason": "refusal",
                             "stop_details": {"type": "refusal", "category": None, "explanation": None}})
    with pytest.raises(ClaudeRefused):
        claude.structured("review", "sys", "hi", Review)
