"""Google Sheets connector for COBROS worksheet."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

import gspread

from src.models.pipeline import PatchAction, PatchActionType
from src.models.sheet import SheetRow

LOGGER = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    """Raised for Google Sheets connectivity errors."""


class SheetsConnector:
    """Read/write connector for the COBROS worksheet."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.credentials_file = str(config.get("credentials_file", "credentials.json"))
        self.spreadsheet_id = config.get("spreadsheet_id") or None
        self.spreadsheet_name = str(config.get("spreadsheet_name", ""))
        self.worksheet_name = str(config.get("worksheet_name", "COBROS"))
        self._client: Any = None
        self._worksheet: Any = None
        self._cache: list[SheetRow] | None = None
        self._raw_cache: list[list[str]] | None = None

    def connect(self) -> "SheetsConnector":
        try:
            self._client = gspread.service_account(filename=self.credentials_file)
            if self.spreadsheet_id:
                spreadsheet = self._client.open_by_key(self.spreadsheet_id)
            else:
                spreadsheet = self._client.open(self.spreadsheet_name)
            self._worksheet = spreadsheet.worksheet(self.worksheet_name)
            return self
        except Exception as error:  # noqa: BLE001
            LOGGER.exception("google sheets connection failed")
            raise ConnectorError("Unable to connect to Google Sheets") from error

    def __enter__(self) -> "SheetsConnector":
        return self.connect()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._client = None
        self._worksheet = None
        self._cache = None
        self._raw_cache = None

    def _require_worksheet(self) -> Any:
        if self._worksheet is None:
            raise ConnectorError("SheetsConnector is not connected")
        return self._worksheet

    @staticmethod
    def _parse_date(value: str | None) -> datetime.date | None:
        if not value:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        try:
            return datetime.strptime(cleaned, "%d/%m/%Y").date()
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_money(value: str | None) -> Decimal:
        if not value:
            return Decimal("0")
        cleaned = value.strip().replace("$", "").replace(" ", "")
        if not cleaned:
            return Decimal("0")
        try:
            normalized = cleaned.replace(".", "").replace(",", ".")
            return Decimal(normalized)
        except Exception:
            return Decimal("0")

    @staticmethod
    def _parse_int(value: str | None) -> int | None:
        if not value:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        # Handle dirty data: "764842/152833", "abc", floats like "12345.0"
        try:
            # Try float first to handle "12345.0" from Sheets numeric cells
            return int(float(cleaned))
        except (ValueError, OverflowError):
            return None

    @staticmethod
    def _cell(row: list[str], index: int) -> str:
        if index >= len(row):
            return ""
        return row[index]

    def read_all_rows(self) -> list[SheetRow]:
        if self._cache is not None:
            return self._cache

        worksheet = self._require_worksheet()
        values: list[list[str]] = worksheet.get_all_values()
        self._raw_cache = values

        parsed: list[SheetRow] = []
        for i, row in enumerate(values[1:], start=2):
            parsed.append(
                SheetRow(
                    row_number=i,
                    organizacion=self._cell(row, 0) or None,
                    curso=self._cell(row, 1) or None,
                    comision=self._cell(row, 2) or None,
                    fecha_movimiento=self._parse_date(self._cell(row, 3)),
                    tipo_movimiento=self._cell(row, 4),
                    dni=self._cell(row, 5),
                    concepto=self._cell(row, 6),
                    monto=self._parse_money(self._cell(row, 7)),
                    medio_pago=self._cell(row, 8),
                    estudiante=self._cell(row, 9) or None,
                    estado_administrativo=self._cell(row, 10) or None,
                    estado_deuda=self._cell(row, 11) or None,
                    id_movimiento_bancario=self._parse_int(self._cell(row, 12)),
                    id_pago_mp=self._parse_int(self._cell(row, 13)),
                )
            )

        self._cache = parsed
        return parsed

    def get_rows_by_commission(self, commission: str) -> list[SheetRow]:
        rows = self.read_all_rows()
        return [row for row in rows if (row.comision or "") == commission]

    def get_rows_by_dni(self, dni: str) -> list[SheetRow]:
        rows = self.read_all_rows()
        return [row for row in rows if row.dni == dni]

    def get_next_empty_row(self) -> int:
        worksheet = self._require_worksheet()
        if self._raw_cache is None:
            self.read_all_rows()
        assert self._raw_cache is not None
        return len(self._raw_cache) + 1

    @staticmethod
    def _next_available_row(worksheet: Any) -> int:
        """Find the next empty row in the worksheet."""
        values = worksheet.col_values(3)  # Column C (comision) — always populated for real rows
        return len(values) + 1

    @staticmethod
    def _ensure_row_capacity(worksheet: Any, last_required_row: int) -> None:
        """Extend the worksheet if writes would go past the current row count."""
        current_rows = int(getattr(worksheet, "row_count", 0) or 0)
        if last_required_row <= current_rows:
            return
        worksheet.add_rows(last_required_row - current_rows)

    @staticmethod
    def _extract_row_values(action: PatchAction) -> list[Any]:
        if not action.new_value:
            return []
        try:
            candidate = json.loads(action.new_value)
            if isinstance(candidate, list):
                return candidate
        except json.JSONDecodeError:
            pass
        return [action.new_value]

    @staticmethod
    def _with_backoff(callable_fn: Any, max_retries: int = 7, base_delay: float = 2.0) -> Any:
        for attempt in range(max_retries):
            try:
                return callable_fn()
            except Exception as error:  # noqa: BLE001
                error_str = str(error)
                # DNS/connection errors are not retryable — fail fast
                if "NameResolutionError" in error_str or "getaddrinfo failed" in error_str:
                    raise
                if attempt == max_retries - 1:
                    raise
                wait_seconds = base_delay * (2**attempt)
                LOGGER.warning("sheets rate-limit retry attempt=%d wait=%.0fs", attempt + 1, wait_seconds)
                time.sleep(wait_seconds)
                last_error = error
        raise last_error  # type: ignore[name-defined]

    def batch_update(self, actions: list[PatchAction]) -> dict[str, Any]:
        worksheet = self._require_worksheet()
        cell_updates: list[dict[str, Any]] = []
        rows_to_delete: list[int] = []
        inserted_rows = 0

        # 1) Apply inserts one-by-one with explicit row growth.
        # This is slower than one giant batch but avoids overwhelming Sheets
        # and guarantees the worksheet has enough rows before writing.
        insert_actions = [a for a in actions if a.action_type == PatchActionType.INSERT_ROW]
        if insert_actions:
            base_row = self._next_available_row(worksheet)
            self._ensure_row_capacity(worksheet, base_row + len(insert_actions) - 1)
            for offset, action in enumerate(insert_actions):
                inserted_rows += 1
                target_row = base_row + offset
                row_values = self._extract_row_values(action)
                raw_cells: list[dict[str, Any]] = []
                date_cells: list[dict[str, Any]] = []
                for col_idx, val in enumerate(row_values):
                    if val is None:
                        continue
                    col_letter = chr(ord("A") + col_idx)
                    cell = {
                        "range": f"{col_letter}{target_row}",
                        "values": [[val]],
                    }
                    # Date column (D) must use USER_ENTERED so Sheets
                    # interprets "07/04/2026" as a date, not text.
                    if col_letter == "D":
                        date_cells.append(cell)
                    else:
                        raw_cells.append(cell)
                if raw_cells:
                    self._with_backoff(
                        lambda u=raw_cells: worksheet.batch_update(
                            [dict(r) for r in u], value_input_option="RAW",
                        )
                    )
                if date_cells:
                    self._with_backoff(
                        lambda u=date_cells: worksheet.batch_update(
                            [dict(r) for r in u], value_input_option="USER_ENTERED",
                        )
                    )
                    time.sleep(1.5)

        # 2) Collect all regular cell updates, separating date columns
        date_updates: list[dict[str, Any]] = []
        for action in actions:
            if action.action_type == PatchActionType.UPDATE_CELL:
                if action.row_number is None or action.column is None:
                    continue
                entry = {
                    "range": f"{action.column}{action.row_number}",
                    "values": [[action.new_value or ""]],
                }
                # Date column (D) must use USER_ENTERED so Sheets
                # interprets "07/04/2026" as a date, not text.
                if action.column == "D":
                    date_updates.append(entry)
                else:
                    cell_updates.append(entry)

            elif action.action_type == PatchActionType.DELETE_ROW:
                if action.row_number is None:
                    continue
                rows_to_delete.append(action.row_number)

        # 3) Send RAW updates in manageable chunks so Sheets
        # does not reinterpret "$109.600" as 109.6.
        update_chunk_size = 100
        for i in range(0, len(cell_updates), update_chunk_size):
            chunk = cell_updates[i : i + update_chunk_size]
            if not chunk:
                continue
            self._with_backoff(
                lambda u=chunk: worksheet.batch_update(
                    [dict(r) for r in u], value_input_option="RAW",
                )
            )
            time.sleep(1.0)

        # 3b) Send date updates with USER_ENTERED so Sheets parses them as dates.
        if date_updates:
            for i in range(0, len(date_updates), update_chunk_size):
                chunk = date_updates[i : i + update_chunk_size]
                if not chunk:
                    continue
                self._with_backoff(
                    lambda u=chunk: worksheet.batch_update(
                        [dict(r) for r in u], value_input_option="USER_ENTERED",
                    )
                )
                time.sleep(1.0)

        # 4) Delete rows bottom-to-top so row numbers stay valid
        if rows_to_delete:
            for row_num in sorted(rows_to_delete, reverse=True):
                self._with_backoff(lambda r=row_num: worksheet.delete_rows(r))
                time.sleep(0.25)

        self._cache = None
        self._raw_cache = None
        return {
            "total_actions": len(actions),
            "inserted_rows": inserted_rows,
            "batched_updates": len(cell_updates),
        }
