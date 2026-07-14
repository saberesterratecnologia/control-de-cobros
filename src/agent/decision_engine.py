"""LLM decision engine for reconciliation discrepancies."""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.context.context_manager import ContextManager
from src.models.pipeline import Discrepancy, LLMDecision
from src.schemas.llm_response import LLMResponse

LOGGER = logging.getLogger(__name__)


class DecisionEngine:
    def __init__(self, config: dict[str, Any], context_manager: ContextManager):
        llm_config = config["llm"]
        self.primary_model = llm_config["primary_model"]
        self.fallback_model = llm_config["fallback_model"]
        self.temperature = llm_config["temperature"]
        self.max_retries = llm_config["max_retries"]
        self.flag_threshold = llm_config.get("confidence_threshold_flagged", 0.75)
        self.context = context_manager
        self.client = None
        self._system_prompt: str | None = None
        self._few_shot_examples: list[dict[str, Any]] | None = None

    def decide(self, discrepancy: Discrepancy, context: dict[str, Any]) -> LLMDecision:
        enriched_context = dict(context)
        self._inject_precedents(discrepancy, enriched_context)

        input_hash = self._compute_input_hash(discrepancy, enriched_context)
        cached = self._check_cache(input_hash)
        if cached is not None:
            return cached

        messages = self._build_prompt(discrepancy, enriched_context)

        last_error: Exception | None = None
        tokens_used = {"prompt_tokens": None, "completion_tokens": None}

        for model in (self.primary_model, self.fallback_model):
            for _ in range(self.max_retries):
                try:
                    raw = self._call_llm(messages, model)
                    usage = raw.get("usage", {})
                    tokens_used = {
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                    }
                    decision = self._parse_response(raw)
                    decision.discrepancy_id = discrepancy.id
                    decision.model_used = model

                    if model == self.primary_model and decision.confidence < self.flag_threshold:
                        LOGGER.info(
                            "low confidence from primary model, escalating",
                            extra={
                                "discrepancy_id": discrepancy.id,
                                "confidence": decision.confidence,
                                "threshold": self.flag_threshold,
                            },
                        )
                        break

                    self._cache_decision(input_hash, decision, raw, tokens_used)
                    return decision
                except (ValidationError, json.JSONDecodeError, KeyError, ValueError) as error:
                    last_error = error
                    LOGGER.warning(
                        "llm response parse/validation failed",
                        extra={"model": model, "discrepancy_id": discrepancy.id},
                    )
                    continue

        if last_error is not None:
            raise RuntimeError("Unable to produce a valid LLM decision") from last_error

        raise RuntimeError("Unable to produce a valid LLM decision")

    @staticmethod
    def _safe_decimal(value: Any) -> Decimal | None:
        if value is None:
            return None
        try:
            cleaned = str(value).replace("$", "").replace(" ", "")
            if "," in cleaned and "." in cleaned:
                cleaned = cleaned.replace(".", "").replace(",", ".")
            elif "," in cleaned:
                cleaned = cleaned.replace(",", ".")
            if not cleaned:
                return None
            return Decimal(cleaned)
        except (InvalidOperation, ValueError, TypeError):
            return None

    @staticmethod
    def _infer_concepto_tipo(discrepancy: Discrepancy, context: dict[str, Any]) -> str:
        pool = " ".join(
            [
                discrepancy.field or "",
                discrepancy.expected_value or "",
                discrepancy.actual_value or "",
                json.dumps(context.get("ambiguous_payment") or {}, ensure_ascii=False),
            ]
        ).casefold()
        if "inscrip" in pool:
            return "inscripcion"
        if "cuota" in pool:
            return "cuota"
        if "pago_unico" in pool or "pago unico" in pool or "único" in pool:
            return "pago_unico"
        return "desconocido"

    def _inject_precedents(self, discrepancy: Discrepancy, context: dict[str, Any]) -> None:
        payment_record = context.get("payment_record") or {}
        prices = context.get("commission_prices") or {}

        monto = self._safe_decimal(payment_record.get("monto"))
        concepto_tipo = self._infer_concepto_tipo(discrepancy, context)
        pricing_inscripcion = self._safe_decimal(prices.get("inscripcion"))
        pricing_cuota = self._safe_decimal(prices.get("cuota"))

        relevant_price: Decimal | None = None
        if concepto_tipo == "inscripcion":
            relevant_price = pricing_inscripcion
        elif concepto_tipo == "cuota":
            relevant_price = pricing_cuota
        elif concepto_tipo == "pago_unico":
            relevant_price = pricing_cuota or pricing_inscripcion

        if monto is None or relevant_price is None or relevant_price <= 0:
            return

        ratio = float(monto / relevant_price)
        similar = self.context.find_similar_resolutions(monto_ratio=ratio, concepto_tipo=concepto_tipo)
        if not similar:
            return

        context["precedents"] = [
            (
                f"En un caso similar (dni={item.get('dni')}, monto={item.get('monto')}, "
                f"ratio={round(float(item.get('monto_ratio', 0)) * 100, 2)}%), "
                f"se resolvió: {item.get('resolution')}"
            )
            for item in similar
        ]

    def _build_prompt(self, discrepancy: Discrepancy, context: dict[str, Any]) -> list[dict[str, str]]:
        system_prompt = self._load_system_prompt()
        examples = self._load_few_shot_examples()

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]

        for example in examples:
            messages.append(
                {
                    "role": "user",
                    "content": json.dumps(example["input"], ensure_ascii=False),
                }
            )
            messages.append(
                {
                    "role": "assistant",
                    "content": json.dumps(example["output"], ensure_ascii=False),
                }
            )

        payload = {
            "discrepancy": {
                "id": discrepancy.id,
                "type": discrepancy.discrepancy_type.value,
                "field": discrepancy.field,
                "expected": discrepancy.expected_value,
                "actual": discrepancy.actual_value,
                "dni": discrepancy.dni,
                "commission": discrepancy.commission,
            },
            "context": {
                "payment_history": context.get("payment_history", []),
                "payment_history_summary": context.get("payment_history_summary", {}),
                "commission_prices": context.get("commission_prices", {}),
                "student_info": context.get("student_info", {}),
                "active_commissions": context.get("active_commissions", []),
                "payment_record": context.get("payment_record"),
                "bank_movement": context.get("bank_movement"),
                "ambiguous_payment": context.get("ambiguous_payment"),
                "ledger_summary": context.get("ledger_summary", {}),
                "sequence_integrity": context.get("sequence_integrity", {}),
                "allocator_diagnostics": context.get("allocator_diagnostics", {}),
                "existing_sheet_rows": context.get("existing_sheet_rows", []),
                "precedents": context.get("precedents", []),
            },
            "instructions": (
                "Respond ONLY as valid JSON. Allowed keys: action, confidence, reasoning, "
                "suggested_value, chosen_candidate_index, suggested_concept, suggested_amount. "
                "Use suggested_concept for v2 allocation choices."
            ),
        }
        messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)})
        return messages

    def _call_llm(self, messages: list[dict[str, str]], model: str) -> dict[str, Any]:
        if self.client is None:
            from openai import OpenAI

            self.client = OpenAI()

        LOGGER.info("llm request", extra={"model": model, "messages": messages})
        response = self.client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=self.temperature,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content or "{}"
        raw = {
            "model": model,
            "content": content,
            "usage": {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", None),
                "completion_tokens": getattr(response.usage, "completion_tokens", None),
                "total_tokens": getattr(response.usage, "total_tokens", None),
            },
        }
        LOGGER.info("llm response", extra={"model": model, "raw": raw})
        return raw

    def _parse_response(self, raw: dict[str, Any]) -> LLMDecision:
        parsed_json = json.loads(raw["content"])
        validated = LLMResponse.model_validate(parsed_json)

        return LLMDecision(
            discrepancy_id="",
            action=validated.action,
            reasoning=validated.reasoning,
            confidence=validated.confidence,
            suggested_value=validated.suggested_value,
            model_used=raw.get("model", "unknown"),
            chosen_candidate_index=validated.chosen_candidate_index,
            suggested_concept=validated.suggested_concept,
            suggested_amount=validated.suggested_amount,
        )

    def _compute_input_hash(self, discrepancy: Discrepancy, context: dict[str, Any]) -> str:
        payload = {
            "discrepancy": discrepancy.model_dump(mode="json"),
            "context": context,
            "prompt": self._load_system_prompt(),
            "few_shot": self._load_few_shot_examples(),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        return sha256(encoded).hexdigest()

    def _check_cache(self, input_hash: str) -> LLMDecision | None:
        row = self.context.get_decision_by_hash(input_hash)
        if row is None:
            return None

        parsed = json.loads(row["decision_json"])
        return LLMDecision(
            discrepancy_id=parsed.get("discrepancy_id", ""),
            action=parsed["action"],
            reasoning=parsed["reasoning"],
            confidence=float(parsed["confidence"]),
            suggested_value=parsed.get("suggested_value"),
            model_used=row["model_used"],
            chosen_candidate_index=parsed.get("chosen_candidate_index"),
            suggested_concept=parsed.get("suggested_concept"),
            suggested_amount=parsed.get("suggested_amount"),
        )

    def _cache_decision(
        self,
        input_hash: str,
        decision: LLMDecision,
        raw: dict[str, Any],
        tokens: dict[str, Any],
    ) -> None:
        current_run = self.context.get_current_run()
        run_id = current_run["id"] if current_run else "adhoc"

        self.context.save_decision(
            run_id=run_id,
            discrepancy_id=None,
            input_hash=input_hash,
            model_used=decision.model_used,
            decision_json=decision.model_dump(mode="json"),
            confidence=decision.confidence,
            raw_response=raw.get("content"),
            prompt_tokens=tokens.get("prompt_tokens"),
            completion_tokens=tokens.get("completion_tokens"),
        )

    def _load_system_prompt(self) -> str:
        if self._system_prompt is not None:
            return self._system_prompt

        prompt_path = Path("config/prompts/conciliation_system.txt")
        self._system_prompt = prompt_path.read_text(encoding="utf-8")
        return self._system_prompt

    def _load_few_shot_examples(self) -> list[dict[str, Any]]:
        if self._few_shot_examples is not None:
            return self._few_shot_examples

        examples_path = Path("config/prompts/few_shot_examples.json")
        self._few_shot_examples = json.loads(examples_path.read_text(encoding="utf-8"))
        return self._few_shot_examples
