"""Patch plan builder for batch writer phase."""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
from uuid import uuid4

from src.context.context_manager import ContextManager
from src.models.pipeline import PatchAction, PatchActionType
from src.models.sheet import ExpectedRow, SheetRow


class PatchBuilder:
    def __init__(self, context_manager: ContextManager):
        self.context = context_manager
        self.actions: list[PatchAction] = []

    @staticmethod
    def _format_money_for_sheet(value: object) -> str:
        """Format a Decimal/numeric value as Argentine-style currency ($XX.XXX).

        Uses Decimal throughout — never converts to float.
        """
        if isinstance(value, Decimal):
            amount = value
        else:
            try:
                amount = Decimal(str(value))
            except Exception:
                return str(value)
        # Round to integer (no cents in the sheet)
        rounded = int(amount.to_integral_value(rounding=ROUND_HALF_UP))
        # Format with dot as thousands separator
        formatted = f"{rounded:,}".replace(",", ".")
        return f"${formatted}"

    @staticmethod
    def _build_delete_snapshot(row: SheetRow) -> str:
        """Build a stable snapshot for delete idempotency.

        Row numbers alone are not stable across runs because inserts/deletes shift
        the sheet. We include the business content so a delete previously applied
        to an old row 7059 does not block deleting a different future row 7059.
        """
        try:
            monto = format(Decimal(str(row.monto)).normalize(), "f")
        except Exception:
            monto = str(row.monto)

        snapshot = {
            "row_number": row.row_number,
            "comision": row.comision.strip() if row.comision else None,
            "fecha_movimiento": row.fecha_movimiento.isoformat() if row.fecha_movimiento else None,
            "tipo_movimiento": row.tipo_movimiento.strip(),
            "dni": row.dni.strip(),
            "concepto": row.concepto.strip(),
            "monto": monto,
            "medio_pago": row.medio_pago.strip() if row.medio_pago else None,
            "id_movimiento_bancario": row.id_movimiento_bancario,
            "id_pago_mp": row.id_pago_mp,
        }
        return json.dumps(snapshot, ensure_ascii=False, sort_keys=True)

    def add_insert(self, expected_row: ExpectedRow, discrepancy_id: str) -> PatchAction | None:
        # Block inserts without a commission — we don't know where to put them
        if not expected_row.comision or not expected_row.comision.strip():
            return None

        # Only populate columns the agent owns (C-I, M-N).
        # Columns A-B have formulas; J-L are populated by other processes.
        # Use None for cells that must not be touched.
        row_payload = [
            None,                                                                     # A: org (formula)
            None,                                                                     # B: curso (formula)
            expected_row.comision.strip(),                                             # C: comision
            expected_row.fecha_movimiento.strftime("%d/%m/%Y"),                        # D: fecha
            expected_row.tipo_movimiento,                                              # E: tipo
            expected_row.dni,                                                          # F: dni
            expected_row.concepto,                                                     # G: concepto
            self._format_money_for_sheet(expected_row.monto),                          # H: monto
            expected_row.medio_pago,                                                   # I: medio
            None,                                                                     # J: estudiante
            None,                                                                     # K: est_academico
            expected_row.estado_administrativo or None,                                # L: est_administrativo
            str(expected_row.id_movimiento_bancario or "") if expected_row.tipo_movimiento == "Cobro" else "",  # M: id_mov
            str(expected_row.id_pago_mp) if expected_row.id_pago_mp else "",           # N: id_pago
        ]

        payload = f"{expected_row.id_pago_mp}|{expected_row.tipo_movimiento}|{expected_row.concepto}"
        action = PatchAction(
            id=str(uuid4()),
            action_type=PatchActionType.INSERT_ROW,
            row_number=None,
            column=None,
            old_value=None,
            new_value=json.dumps(row_payload, ensure_ascii=False),
            idempotency_key=sha256(payload.encode("utf-8")).hexdigest(),
            source_discrepancy_id=discrepancy_id,
            status="planned",
        )
        self.actions.append(action)
        return action

    @staticmethod
    def _format_date_for_sheet(value: str) -> str:
        """Convert ISO date (YYYY-MM-DD) to DD/MM/YYYY for the sheet."""
        if not value or not value.strip():
            return value
        cleaned = value.strip().split("T")[0]  # drop time component if present
        try:
            from datetime import datetime as _dt
            parsed = _dt.strptime(cleaned, "%Y-%m-%d")
            return parsed.strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            return value

    def add_update(
        self,
        row_number: int,
        column: str,
        old_value: str,
        new_value: str,
        discrepancy_id: str,
    ) -> PatchAction:
        formatted_old = old_value
        formatted_new = new_value
        if column == "D":
            formatted_new = self._format_date_for_sheet(new_value)
        if column == "H":
            formatted_old = self._format_money_for_sheet(old_value)
            formatted_new = self._format_money_for_sheet(new_value)

        payload = f"{row_number}|{column}|{formatted_old}|{formatted_new}"
        action = PatchAction(
            id=str(uuid4()),
            action_type=PatchActionType.UPDATE_CELL,
            row_number=row_number,
            column=column,
            old_value=formatted_old,
            new_value=formatted_new,
            idempotency_key=sha256(payload.encode("utf-8")).hexdigest(),
            source_discrepancy_id=discrepancy_id,
            status="planned",
        )
        self.actions.append(action)
        return action

    def add_delete(self, row: SheetRow, discrepancy_id: str) -> PatchAction:
        snapshot = self._build_delete_snapshot(row)
        payload = f"delete|{snapshot}"
        action = PatchAction(
            id=str(uuid4()),
            action_type=PatchActionType.DELETE_ROW,
            row_number=row.row_number,
            column=None,
            old_value=snapshot,
            new_value=None,
            idempotency_key=sha256(payload.encode("utf-8")).hexdigest(),
            source_discrepancy_id=discrepancy_id,
            status="planned",
        )
        self.actions.append(action)
        return action

    def add_flag(self, row_number: int, discrepancy_id: str, reason: str) -> PatchAction:
        action = PatchAction(
            id=str(uuid4()),
            action_type=PatchActionType.FLAG_REVIEW,
            row_number=row_number,
            column=None,
            old_value=None,
            new_value=reason,
            source_discrepancy_id=discrepancy_id,
            status="planned",
        )
        self.actions.append(action)
        return action

    def build_plan(self) -> list[PatchAction]:
        plan: list[PatchAction] = []
        seen_keys: set[str] = set()
        for action in self.actions:
            if action.idempotency_key in seen_keys:
                action.status = "skipped"
                continue
            if self.context.is_already_applied(action.idempotency_key):
                action.status = "skipped"
                continue
            seen_keys.add(action.idempotency_key)
            plan.append(action)
        return plan

    def get_summary(self) -> dict:
        by_type = {
            "insert_row": 0,
            "update_cell": 0,
            "flag_review": 0,
            "delete_row": 0,
        }
        by_status: dict[str, int] = {}
        for action in self.actions:
            by_type[action.action_type.value] = by_type.get(action.action_type.value, 0) + 1
            by_status[action.status] = by_status.get(action.status, 0) + 1
        return {
            "total_actions": len(self.actions),
            "by_type": by_type,
            "by_status": by_status,
        }
