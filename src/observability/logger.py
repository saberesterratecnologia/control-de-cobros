"""Structured JSON logging helpers."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "event"):
            payload["event"] = record.event
        if hasattr(record, "data"):
            payload["data"] = record.data
        return json.dumps(payload, ensure_ascii=False, default=str)


class StructuredLogger:
    """JSON-formatted structured logger."""

    def __init__(self, name: str, log_file: str | None = None):
        self._logger = logging.getLogger(name)
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False

        if not self._logger.handlers:
            formatter = JsonFormatter()
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            self._logger.addHandler(stream_handler)

            if log_file:
                file_handler = logging.FileHandler(log_file, encoding="utf-8")
                file_handler.setFormatter(formatter)
                self._logger.addHandler(file_handler)

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        self._logger.info(event, extra={"event": event, "data": data})

    def log_run_start(self, config: dict) -> None:
        self._emit("run_start", {"config": config})

    def log_commission_start(self, commission: str, student_count: int) -> None:
        self._emit("commission_start", {"commission": commission, "student_count": student_count})

    def log_discrepancy(self, discrepancy: Any) -> None:
        payload = discrepancy.model_dump(mode="json") if hasattr(discrepancy, "model_dump") else discrepancy
        self._emit("discrepancy", {"discrepancy": payload})

    def log_llm_call(self, model: str, tokens: dict, decision: Any) -> None:
        decision_payload = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else decision
        self._emit("llm_call", {"model": model, "tokens": tokens, "decision": decision_payload})

    def log_patch_applied(self, action: Any) -> None:
        action_payload = action.model_dump(mode="json") if hasattr(action, "model_dump") else action
        self._emit("patch_applied", {"action": action_payload})

    def log_run_summary(self, summary: dict) -> None:
        self._emit("run_summary", {"summary": summary})
