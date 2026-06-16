"""Models used by reconciliation comparison and decision pipeline."""

from __future__ import annotations

from decimal import Decimal
from enum import Enum
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.models.sheet import ExpectedRow, SheetRow
from src.models.source import BankMovement, Payment


class DiscrepancyType(str, Enum):
    MISSING_ROW = "missing_row"
    WRONG_VALUE = "wrong_value"
    EXTRA_ROW = "extra_row"
    DUPLICATE = "duplicate"


class Resolution(str, Enum):
    AUTO_FIX = "auto_fix"
    LLM_DECIDED = "llm_decided"
    PENDING_REVIEW = "pending_review"
    SKIPPED = "skipped"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Discrepancy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    commission: str
    dni: str
    discrepancy_type: DiscrepancyType
    field: str | None
    expected_value: str | None
    actual_value: str | None
    expected_row: ExpectedRow | None
    actual_row: SheetRow | None
    confidence: float
    severity: Severity
    resolution: Resolution | None
    resolved_by: str | None


class LLMDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discrepancy_id: str
    action: str
    reasoning: str
    confidence: float
    suggested_value: str | None
    model_used: str
    # v2 allocation-aware fields (from LLM response schema)
    chosen_candidate_index: int | None = None
    suggested_concept: str | None = None
    suggested_amount: str | None = None


class PatchActionType(str, Enum):
    INSERT_ROW = "insert_row"
    UPDATE_CELL = "update_cell"
    DELETE_ROW = "delete_row"
    FLAG_REVIEW = "flag_review"


class PatchAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    action_type: PatchActionType
    row_number: int | None
    column: str | None
    old_value: str | None
    new_value: str | None
    idempotency_key: str = ""
    source_discrepancy_id: str
    status: str

    @model_validator(mode="after")
    def ensure_idempotency_key(self) -> "PatchAction":
        if self.idempotency_key:
            return self

        payload = "|".join(
            [
                self.action_type.value,
                str(self.row_number),
                str(self.column),
                str(self.old_value),
                str(self.new_value),
                self.source_discrepancy_id,
            ]
        )
        self.idempotency_key = sha256(payload.encode("utf-8")).hexdigest()
        return self


class ConciliatedPayment(BaseModel):
    """A payment paired with its bank movement (if conciliated)."""

    model_config = ConfigDict(extra="forbid")

    payment: Payment
    movement: BankMovement | None = None
    conciliated_by: str = "existing"  # "existing", "auto", "llm", "unconciliated"


class AllocationCandidate(BaseModel):
    """One possible interpretation of what a payment pays for."""

    model_config = ConfigDict(extra="forbid")

    concept: str  # "Inscripción", "Cuota 3", "Pago Único", etc.
    amount: Decimal
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str


class AmbiguousPayment(BaseModel):
    """A payment where deterministic allocation failed — needs LLM."""

    model_config = ConfigDict(extra="forbid")

    payment: ConciliatedPayment
    candidates: list[AllocationCandidate]


class Allocation(BaseModel):
    """A resolved allocation: payment mapped to a specific concept."""

    model_config = ConfigDict(extra="forbid")

    payment: ConciliatedPayment
    concept: str  # "Inscripción", "Cuota 1", "Cuota 2", "Pago Único"
    amount: Decimal
    generates_venta: bool = True  # always True
    generates_cobro: bool = True  # True only when conciliation is persisted in SQL


class AllocationResult(BaseModel):
    """Complete result of allocating all payments for one student/commission."""

    model_config = ConfigDict(extra="forbid")

    allocated: list[Allocation] = Field(default_factory=list)
    ambiguous: list[AmbiguousPayment] = Field(default_factory=list)
    next_venta: ExpectedRow | None = None  # The ONE next expected payment


class Anomaly(BaseModel):
    """An anomalous row detected in the sheet during normalization."""

    model_config = ConfigDict(extra="forbid")

    row_number: int
    anomaly_type: str  # "cobro_no_aplica", "venta_with_movement", "invalid_id", etc.
    description: str
    severity: Severity = Severity.WARNING
