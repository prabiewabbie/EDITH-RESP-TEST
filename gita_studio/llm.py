"""Thin wrapper over the Claude API: every stage asks for a typed, schema-validated result."""
from __future__ import annotations

import logging
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from .config import Config

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeRefused(RuntimeError):
    """The model (and its fallback) declined the request."""


class Claude:
    def __init__(self, config: Config, client: anthropic.Anthropic | None = None):
        self.config = config
        # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN or an `ant auth login` profile.
        self.client = client or anthropic.Anthropic(max_retries=4)

    def structured(self, stage: str, system: str, prompt: str, schema: type[T], max_tokens: int = 16000) -> T:
        response = self.client.beta.messages.parse(
            model=self.config.model.id,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            output_config={"effort": self.config.model.effort_for(stage)},
            # On a safety decline, the API re-runs the request on Anthropic's recommended fallback model.
            fallbacks="default",
            betas=[FALLBACK_BETA],
            cache_control={"type": "ephemeral"},
        )
        log.info(
            "stage=%s request_id=%s in=%s out=%s cache_read=%s",
            stage,
            response._request_id,
            response.usage.input_tokens,
            response.usage.output_tokens,
            response.usage.cache_read_input_tokens,
        )
        if response.stop_reason == "refusal":
            category = response.stop_details.category if response.stop_details else None
            raise ClaudeRefused(f"{stage}: request declined (category={category})")
        if response.stop_reason == "max_tokens":
            raise RuntimeError(f"{stage}: output truncated at max_tokens={max_tokens}")
        if response.parsed_output is None:
            raise RuntimeError(f"{stage}: no structured output returned")
        return response.parsed_output
