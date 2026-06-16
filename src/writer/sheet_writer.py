"""Execute patch plans against Google Sheets connector."""

from __future__ import annotations

import json
import logging
import time
from decimal import Decimal
from typing import Any

from src.connectors.sheets import SheetsConnector
from src.context.context_manager import ContextManager
from src.models.pipeline import PatchAction, PatchActionType

LOGGER = logging.getLogger(__name__)


class SheetWriter:
    def __init__(self, sheets_connector: SheetsConnector, context_manager: ContextManager, config: dict):
        self.sheets = sheets_connector
        self.context = context_manager
        self.batch_size = config["agent"]["batch_size"]
        self.checkpoint_interval = config["agent"]["checkpoint_interval"]

    @staticmethod
    def _format_money_arg(value: str | None) -> str:
        if value is None:
            return ""
        cleaned = str(value).replace("$", "").replace(" ", "").replace(".", "").replace(",", ".")
        try:
            amount = Decimal(cleaned)
        except Exception:  # noqa: BLE001
            return str(value)
        return "$" + f"{int(amount):,}".replace(",", ".")

    def execute_dry_run(self, plan: list[PatchAction]) -> str:
        inserts = [a for a in plan if a.action_type == PatchActionType.INSERT_ROW]
        updates = [a for a in plan if a.action_type == PatchActionType.UPDATE_CELL]
        flags = [a for a in plan if a.action_type == PatchActionType.FLAG_REVIEW]

        lines = [
            "=== DRY RUN REPORT ===",
            f"Total actions: {len(plan)}",
            f"- Inserts: {len(inserts)}",
            f"- Updates: {len(updates)}",
            f"- Flags: {len(flags)}",
            "",
        ]

        for action in inserts:
            values = self._format_row_for_insert(action)
            comision = values[2] if len(values) > 2 else ""
            tipo = values[4] if len(values) > 4 else ""
            dni = values[5] if len(values) > 5 else ""
            concepto = values[6] if len(values) > 6 else ""
            monto = values[7] if len(values) > 7 else ""
            lines.append(
                f"[INSERT] {tipo} | Commission: {comision} | DNI: {dni} | {concepto} | {monto}"
            )

        for action in updates:
            lines.append(
                "[UPDATE] Row "
                f"{action.row_number} | Column {action.column} | "
                f"{self._format_money_arg(action.old_value)} → {self._format_money_arg(action.new_value)} | "
                f"Reason: {action.source_discrepancy_id}"
            )

        for action in flags:
            lines.append(
                f"[FLAG] Row {action.row_number} | Reason: {action.new_value or action.source_discrepancy_id}"
            )

        return "\n".join(lines)

    def execute_live(self, plan: list[PatchAction]) -> dict:
        pending = [a for a in plan if not self.context.is_already_applied(a.idempotency_key)]
        summary = {
            "total": len(plan),
            "attempted": len(pending),
            "applied": 0,
            "failed": 0,
            "skipped": len(plan) - len(pending),
            "errors": [],
        }
        if not pending:
            return summary

        run = self.context.get_current_run()
        run_id = run["id"] if run else "adhoc"
        sheet_snapshot = self.sheets._require_worksheet().get_all_values()
        next_insert_row = len(sheet_snapshot) + 1

        patch_db_ids: dict[str, int] = {}
        for action in pending:
            patch_db_ids[action.id] = self.context.save_patch(run_id, action)

        ordered = [
            *[a for a in pending if a.action_type == PatchActionType.INSERT_ROW],
            *[a for a in pending if a.action_type == PatchActionType.UPDATE_CELL],
            *[a for a in pending if a.action_type == PatchActionType.DELETE_ROW],
            *[a for a in pending if a.action_type == PatchActionType.FLAG_REVIEW],
        ]

        processed = 0
        for i in range(0, len(ordered), self.batch_size):
            if i > 0:
                time.sleep(2)  # Throttle between batches to avoid rate limit
            batch = ordered[i : i + self.batch_size]
            results = self._apply_batch(batch)
            for result in results:
                action = result["action"]
                patch_id = patch_db_ids[action.id]
                if result["ok"]:
                    action.status = "applied"
                    self.context.update_patch_status(patch_id, "applied")
                    self._save_action_rollback_snapshot(
                        run_id=run_id,
                        action=action,
                        sheet_snapshot=sheet_snapshot,
                        next_insert_row=next_insert_row,
                    )
                    if action.action_type == PatchActionType.INSERT_ROW:
                        next_insert_row += 1
                    summary["applied"] += 1
                else:
                    action.status = "failed"
                    self.context.update_patch_status(patch_id, "error")
                    summary["failed"] += 1
                    summary["errors"].append(result["error"])
                processed += 1
                if processed % self.checkpoint_interval == 0:
                    self.context.save_checkpoint(
                        run_id=run_id,
                        phase="write",
                        checkpoint_data={"processed": processed, "applied": summary["applied"]},
                    )

        return summary

    @staticmethod
    def _column_to_index(column: str | None) -> int | None:
        if not column:
            return None
        normalized = column.strip().upper()
        if not normalized or not normalized.isalpha():
            return None
        idx = 0
        for ch in normalized:
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
        return idx - 1

    @staticmethod
    def _row_from_snapshot(sheet_snapshot: list[list[str]], row_number: int | None) -> list[str] | None:
        if row_number is None:
            return None
        if row_number <= 0:
            return None
        idx = row_number - 1
        if idx >= len(sheet_snapshot):
            return None
        row = sheet_snapshot[idx]
        return ["" if cell is None else str(cell) for cell in row]

    def _save_action_rollback_snapshot(
        self,
        run_id: str,
        action: PatchAction,
        sheet_snapshot: list[list[str]],
        next_insert_row: int,
    ) -> None:
        if action.action_type == PatchActionType.UPDATE_CELL:
            row = self._row_from_snapshot(sheet_snapshot, action.row_number)
            col_idx = self._column_to_index(action.column)
            old_value = ""
            if row is not None and col_idx is not None and col_idx < len(row):
                old_value = row[col_idx]
            self.context.save_rollback_snapshot(
                run_id=run_id,
                action_id=f"{action.id}|{action.column or ''}",
                action_type=action.action_type.value,
                row_number=action.row_number,
                row_snapshot=old_value,
            )
            return

        if action.action_type == PatchActionType.DELETE_ROW:
            row = self._row_from_snapshot(sheet_snapshot, action.row_number) or []
            self.context.save_rollback_snapshot(
                run_id=run_id,
                action_id=action.id,
                action_type=action.action_type.value,
                row_number=action.row_number,
                row_snapshot=row,
            )
            return

        if action.action_type == PatchActionType.INSERT_ROW:
            inserted_row = self._format_row_for_insert(action)
            self.context.save_rollback_snapshot(
                run_id=run_id,
                action_id=action.id,
                action_type=action.action_type.value,
                row_number=next_insert_row,
                row_snapshot=inserted_row,
            )

    @staticmethod
    def _rows_equal(left: list[str], right: list[str]) -> bool:
        max_len = max(len(left), len(right))
        for i in range(max_len):
            l = left[i] if i < len(left) else ""
            r = right[i] if i < len(right) else ""
            if (l or "") != (r or ""):
                return False
        return True

    def execute_rollback(self, run_id: str) -> dict[str, Any]:
        snapshots = self.context.get_rollback_snapshots(run_id)
        worksheet = self.sheets._require_worksheet()
        summary: dict[str, Any] = {"restored": 0, "failed": 0, "errors": []}

        deletes = [s for s in snapshots if s.get("action_type") == PatchActionType.DELETE_ROW.value]
        updates = [s for s in snapshots if s.get("action_type") == PatchActionType.UPDATE_CELL.value]
        inserts = [s for s in snapshots if s.get("action_type") == PatchActionType.INSERT_ROW.value]

        # 1) Undo deletes first — reinsert rows bottom-up.
        for snap in sorted(deletes, key=lambda s: int(s.get("row_number") or 0), reverse=True):
            try:
                row_number = snap.get("row_number")
                raw_snapshot = snap.get("row_snapshot")
                row_values = json.loads(raw_snapshot) if raw_snapshot else []
                if not isinstance(row_values, list):
                    row_values = []
                if row_number is None:
                    worksheet.append_row(row_values, value_input_option="RAW")
                else:
                    worksheet.insert_row(row_values, int(row_number), value_input_option="RAW")
                summary["restored"] += 1
            except Exception as error:  # noqa: BLE001
                summary["failed"] += 1
                summary["errors"].append(f"delete_undo snapshot_id={snap.get('id')}: {error}")

        # 2) Undo updates.
        for snap in sorted(updates, key=lambda s: int(s.get("id") or 0), reverse=True):
            try:
                row_number = snap.get("row_number")
                if row_number is None:
                    raise ValueError("missing row_number for update rollback")
                old_value = ""
                if snap.get("row_snapshot"):
                    old_value = json.loads(snap["row_snapshot"])
                action_id = str(snap.get("action_id") or "")
                _, _, column = action_id.partition("|")
                if not column:
                    raise ValueError("cannot infer column for update rollback")
                worksheet.update(
                    f"{column}{int(row_number)}",
                    [[old_value]],
                    value_input_option="RAW",
                )
                summary["restored"] += 1
            except Exception as error:  # noqa: BLE001
                summary["failed"] += 1
                summary["errors"].append(f"update_undo snapshot_id={snap.get('id')}: {error}")

        # 3) Undo inserts last — delete inserted rows by matching content.
        for snap in sorted(inserts, key=lambda s: int(s.get("row_number") or 0), reverse=True):
            try:
                expected = json.loads(snap["row_snapshot"]) if snap.get("row_snapshot") else []
                if not isinstance(expected, list):
                    expected = []
                all_rows = worksheet.get_all_values()
                match_row_number: int | None = None
                for idx, row in enumerate(all_rows[1:], start=2):
                    if self._rows_equal(row, expected):
                        match_row_number = idx
                        break
                if match_row_number is None:
                    raise ValueError("inserted row not found by content")
                worksheet.delete_rows(match_row_number)
                summary["restored"] += 1
            except Exception as error:  # noqa: BLE001
                summary["failed"] += 1
                summary["errors"].append(f"insert_undo snapshot_id={snap.get('id')}: {error}")

        return summary

    def _format_row_for_insert(self, action: PatchAction) -> list[str]:
        if not action.new_value:
            return []
        try:
            parsed = json.loads(action.new_value)
        except json.JSONDecodeError:
            return [action.new_value]
        if isinstance(parsed, list):
            return ["" if cell is None else str(cell) for cell in parsed]
        return [str(parsed)]

    def _apply_batch(self, batch: list[PatchAction]) -> list[dict]:
        results: list[dict[str, Any]] = []
        # Separate flags (no-op) from real actions
        real_actions = [a for a in batch if a.action_type != PatchActionType.FLAG_REVIEW]
        flags = [a for a in batch if a.action_type == PatchActionType.FLAG_REVIEW]

        for flag in flags:
            results.append({"action": flag, "ok": True, "error": None})

        if real_actions:
            try:
                self.sheets.batch_update(real_actions)
                for action in real_actions:
                    results.append({"action": action, "ok": True, "error": None})
            except Exception as error:  # noqa: BLE001
                LOGGER.exception("batch write failed, falling back to individual writes")
                # Fallback: try one by one so partial success is captured
                for action in real_actions:
                    try:
                        self.sheets.batch_update([action])
                        results.append({"action": action, "ok": True, "error": None})
                    except Exception as individual_error:  # noqa: BLE001
                        LOGGER.exception("individual write failed", extra={"patch_id": action.id})
                        results.append({"action": action, "ok": False, "error": str(individual_error)})

        return results
