"""Schema and model for structured LLM responses."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

LLM_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["fix", "skip", "flag_review"],
        },
        "chosen_candidate_index": {
            "type": ["integer", "null"],
        },
        "suggested_concept": {
            "type": ["string", "null"],
        },
        "suggested_amount": {
            "type": ["string", "null"],
        },
        "suggested_value": {
            "type": ["string", "null"],
        },
        "confidence": {
            "type": ["number", "null"],
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "reasoning": {
            "type": "string",
        },
    },
    "required": ["action", "confidence", "reasoning"],
    "additionalProperties": False,
}


class LLMResponse(BaseModel):
    action: Literal["fix", "skip", "flag_review"]
    chosen_candidate_index: int | None = None
    suggested_concept: str | None = None
    suggested_amount: str | None = None
    suggested_value: str | None = None
    confidence: float | None = Field(default=0.0, ge=0.0, le=1.0)
    reasoning: str

    @model_validator(mode="after")
    def normalize_compat_fields(self) -> "LLMResponse":
        """Support both the legacy and v2 allocation-based response shapes."""
        if self.suggested_value is None and self.suggested_concept is not None:
            self.suggested_value = self.suggested_concept
        if self.confidence is None:
            self.confidence = 0.0
        return self
