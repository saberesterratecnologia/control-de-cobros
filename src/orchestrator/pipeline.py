"""Main reconciliation pipeline orchestration (v2)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from itertools import count
from typing import Any

LOGGER = logging.getLogger(__name__)

from src.agent.decision_engine import DecisionEngine
from src.comparator.scorer import ConfidenceScorer
from src.comparator.sheet_reconciler import SheetReconciler
from src.connectors.sheets import SheetsConnector
from src.connectors.sqlserver import SQLServerConnector
from src.context.context_manager import ContextManager
from src.models.pipeline import (
    Allocation,
    AmbiguousPayment,
    ConciliatedPayment,
    Discrepancy,
    DiscrepancyType,
    Resolution,
    Severity,
)
from src.models.sheet import ExpectedRow, SheetRow
from src.models.source import BankMovement, Commission, Payment, Student
from src.rules.allocation_engine import AllocationEngine, Ledger
from src.rules.mappers import map_cobro_medio, map_estado_administrativo, map_medio
from src.rules.normalizer import SheetNormalizer
from src.reviews.review_manager import ReviewManager
from src.writer.patch_builder import PatchBuilder
from src.writer.sheet_writer import SheetWriter


BLOCKING_GUARD_PREFIXES: frozenset[str] = frozenset({"cuota_exceeds_total"})


@dataclass
class _RunCounters:
    cobros_blocked: int = 0
    commissions_processed: int = 0
    students_processed: int = 0
    discrepancies_total: int = 0
    auto_fix: int = 0
    llm_decided: int = 0
    pending_review: int = 0
    cleanup_tasks: int = 0
    sheet_anomalies: int = 0
    errors: int = 0


class ConciliationPipeline:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.context = ContextManager(config["sqlite"]["db_path"])
        self.sql = SQLServerConnector(config["database"])
        self.sheets = SheetsConnector(config["sheets"])
        self.normalizer = SheetNormalizer()
        self.reconciler = SheetReconciler()
        self._discrepancy_ids = count(1)
        self.scorer = ConfidenceScorer()
        self.patch_builder = PatchBuilder(self.context)
        self.decision_engine = DecisionEngine(config, self.context)
        self.sheet_writer = SheetWriter(self.sheets, self.context, config)
        self.review_manager = ReviewManager(self.sheets, self.context, config)
        self._run_id: str | None = None
        self._counters = _RunCounters()
        self._cleanup_tasks: dict[str, dict[str, Any]] = {}

        llm = config.get("llm", {})
        self.auto_threshold = float(llm.get("confidence_threshold_auto", 0.90))
        self.llm_threshold = float(llm.get("confidence_threshold_flagged", 0.75))

    def run(self, commission_filter: str | None = None, dry_run: bool = True, skip_reviews: bool = False, force_reprocess: bool = False) -> dict[str, Any]:
        mode = "dry_run" if dry_run else "live"

        self._prepare_run_state()

        with self.context, self.sql.connect(), self.sheets.connect():
            self._run_id = self.context.start_run(mode=mode, config_snapshot=self.config)
            try:
                self.review_manager.sync_cleanup_statuses()
                self.review_manager.sync_resolutions()
                year = int(self.config["agent"]["year"])
                id_organizacion = int(self.config["agent"].get("id_organizacion", 2))
                commissions = self.sql.get_active_commissions(year=year, id_organizacion=id_organizacion)
                if commission_filter:
                    commissions = [
                        c for c in commissions
                        if commission_filter.strip().casefold() in c.nombre.strip().casefold()
                    ]

                tracked_commissions = {c.nombre.strip() for c in commissions}
                cumulative_write_result: dict[str, Any] = {
                    "applied": 0, "failed": 0, "skipped": 0, "errors": [],
                }
                cumulative_patch_summary: dict[str, Any] = {
                    "total_actions": 0,
                    "by_type": {
                        "insert_row": 0,
                        "update_cell": 0,
                        "flag_review": 0,
                        "delete_row": 0,
                    },
                    "by_status": {},
                }
                processed_anomaly_rows: set[int] = set()

                for commission in commissions:
                    # Read sheet fresh for each commission so we see prior writes
                    raw_sheet_rows = self.sheets.read_all_rows()
                    normalized_sheet_rows, anomalies = self.normalizer.normalize(raw_sheet_rows)

                    # Clear patch builder for this commission
                    self.patch_builder.actions.clear()

                    self._process_commission(commission, normalized_sheet_rows, dry_run=dry_run, force_reprocess=force_reprocess)

                    # Flush patches immediately for this commission
                    patch_plan = self.patch_builder.build_plan()
                    self._merge_patch_summary(cumulative_patch_summary, self.patch_builder.get_summary())
                    if patch_plan:
                        if dry_run:
                            LOGGER.info(
                                "dry-run %s: %d actions planned",
                                commission.nombre.strip(),
                                len(patch_plan),
                            )
                        else:
                            result = self.sheet_writer.execute_live(patch_plan)
                            cumulative_write_result["applied"] += result.get("applied", 0)
                            cumulative_write_result["failed"] += result.get("failed", 0)
                            cumulative_write_result["skipped"] += result.get("skipped", 0)
                            cumulative_write_result["errors"].extend(result.get("errors", []))
                            LOGGER.info(
                                "live %s: applied=%d failed=%d",
                                commission.nombre.strip(),
                                result.get("applied", 0),
                                result.get("failed", 0),
                            )

                    for anomaly in anomalies:
                        if anomaly.row_number in processed_anomaly_rows:
                            continue
                        if not self._should_track_anomaly(
                            anomaly=anomaly,
                            sheet_rows=normalized_sheet_rows,
                            tracked_commissions=tracked_commissions,
                        ):
                            continue
                        row = next((r for r in normalized_sheet_rows if r.row_number == anomaly.row_number), None)
                        cleanup_context = anomaly.model_dump(mode="json")
                        if row is not None:
                            cleanup_context.update(
                                {
                                    "commission": (row.comision or "").strip(),
                                    "dni": row.dni.strip(),
                                    "concepto": row.concepto,
                                    "monto": str(row.monto),
                                    "id_movimiento_bancario": row.id_movimiento_bancario,
                                }
                            )
                        self._record_cleanup_tasks(
                            reason=f"anomaly:{anomaly.anomaly_type}",
                            context_json=cleanup_context,
                        )
                        for review in self.context.get_open_anomaly_reviews_for_rows([anomaly.row_number]):
                            self.context.close_review(review["id"], "auto_close:moved_to_cleanup")
                        processed_anomaly_rows.add(anomaly.row_number)
                        self._counters.sheet_anomalies += 1

                if self._cleanup_tasks:
                    self.review_manager.upsert_cleanup_tasks(self._run_id, list(self._cleanup_tasks.values()))
                    self._counters.cleanup_tasks = len(self._cleanup_tasks)

                summary = self._generate_summary(self._run_id)
                summary["patch_summary"] = cumulative_patch_summary
                if dry_run:
                    summary["writer"] = {"mode": "dry_run"}
                else:
                    summary["writer"] = cumulative_write_result

                if not skip_reviews:
                    # Export the full open backlog on every run so REVISIONES
                    # can self-heal after manual clears or skipped exports.
                    summary["reviews_export"] = self.review_manager.export_to_sheet()
                    summary["cleanup_export"] = self.review_manager.export_cleanup_to_sheet()
                else:
                    summary["reviews_export"] = {"exported": 0, "skipped": 0, "skipped_by_flag": True}
                    summary["cleanup_export"] = {"exported": 0, "skipped": 0, "skipped_by_flag": True}

                self.context.end_run(self._run_id, status="completed", summary=summary)
                return summary
            except Exception as error:  # noqa: BLE001
                self._counters.errors += 1
                summary = self._generate_summary(self._run_id)
                summary["error"] = str(error)
                self.context.end_run(self._run_id, status="failed", summary=summary)
                raise

    def sync_reviews(self) -> dict[str, Any]:
        with self.context, self.sheets.connect():
            return self.review_manager.sync_resolutions()

    def export_reviews(self, run_id: str | None = None) -> dict[str, Any]:
        with self.context, self.sheets.connect():
            return self.review_manager.export_to_sheet(run_id)

    def rollback(self, run_id: str) -> dict[str, Any]:
        with self.context, self.sheets.connect():
            run = self.context.get_run(run_id)
            if run is None:
                raise ValueError(f"Run not found: {run_id}")
            return self.sheet_writer.execute_rollback(run_id)

    def _prepare_run_state(self) -> None:
        self._run_id = None
        self._counters = _RunCounters()
        self._discrepancy_ids = count(1)
        self.patch_builder.actions.clear()
        self._cleanup_tasks.clear()

    @staticmethod
    def _merge_patch_summary(target: dict[str, Any], source: dict[str, Any]) -> None:
        target["total_actions"] += source.get("total_actions", 0)
        for key, value in source.get("by_type", {}).items():
            target["by_type"][key] = target["by_type"].get(key, 0) + value
        for key, value in source.get("by_status", {}).items():
            target["by_status"][key] = target["by_status"].get(key, 0) + value

    def _process_commission(self, commission: Commission, sheet_rows: list[SheetRow], dry_run: bool = True, force_reprocess: bool = False) -> list[Discrepancy]:
        discrepancies: list[Discrepancy] = []
        students = self.sql.get_students(commission.id_comision)
        self._counters.commissions_processed += 1

        if not students:
            assert self._run_id is not None
            self.context.save_checkpoint(
                run_id=self._run_id,
                phase="commission",
                commission_name=commission.nombre,
                checkpoint_data={"commission": commission.nombre, "students": 0, "discrepancies": 0},
                status="done",
            )
            return discrepancies

        for student in students:
            student_discrepancies, _ = self._process_student(student, commission, sheet_rows, dry_run=dry_run, force_reprocess=force_reprocess)
            discrepancies.extend(student_discrepancies)

        # NOTE: _clean_orphaned_rows is intentionally skipped under the
        # insert-only policy.  Rows for students who left the commission
        # are left as-is; cleanup is a supervised manual operation.

        assert self._run_id is not None
        self.context.save_checkpoint(
            run_id=self._run_id,
            phase="commission",
            commission_name=commission.nombre,
            checkpoint_data={
                "commission": commission.nombre,
                "students": len(students),
                "discrepancies": len(discrepancies),
            },
            status="done",
        )
        return discrepancies

    def _process_student(
        self,
        student: Student,
        commission: Commission,
        sheet_rows: list[SheetRow],
        dry_run: bool = True,
        force_reprocess: bool = False,
    ) -> tuple[list[Discrepancy], list[Any]]:
        self._counters.students_processed += 1

        allocator = AllocationEngine(commission)
        payment_year = commission.fecha_inicio.year if commission.fecha_inicio else int(self.config["agent"]["year"])

        # SOLE data source: pre-conciliated pairs
        conciliated_pairs = self.sql.get_conciliated_payments(
            student.id_persona,
            year=payment_year,
            id_organizacion=commission.id_organizacion,
        )

        # --- Flag uncontrolled payments from conciliated set ---
        assert self._run_id is not None
        for payment, _movement in conciliated_pairs:
            if not payment.controlado:
                self._counters.pending_review += 1
                self.context.save_pending_review(
                    run_id=self._run_id,
                    discrepancy_id=None,
                    reason="pago_no_controlado",
                    context_json={
                        "commission": commission.nombre.strip(),
                        "dni": student.dni.strip(),
                        "payment_id": payment.id_pago_mp,
                        "monto": str(payment.monto),
                        "fecha": payment.fecha.date().isoformat() if payment.fecha else None,
                        "controlado": payment.controlado,
                        "controlado_auto": payment.controlado_auto,
                    },
                )

        # Build ConciliatedPayment directly — no matching, no enrichment
        enriched_conciliated = [
            ConciliatedPayment(payment=p, movement=m, conciliated_by="existing")
            for p, m in conciliated_pairs
        ]
        enriched_conciliated = [self._ensure_student_dni(cp, student.dni) for cp in enriched_conciliated]

        all_payments = [cp.payment for cp in enriched_conciliated]

        actual_rows = [
            row
            for row in sheet_rows
            if (row.comision or "").strip() == commission.nombre.strip() and row.dni.strip() == student.dni.strip()
        ]

        # --- Guard: invalid existing sequence in sheet ---
        # If the student's current Venta rows already show an impossible /
        # suspicious sequence, evaluate severity.  Only reasons whose prefix
        # is in BLOCKING_GUARD_PREFIXES halt allocation; the rest save a
        # review for human awareness and continue processing.
        if not force_reprocess:
            invalid_reasons = self._detect_invalid_sheet_sequence(commission, actual_rows)
            if invalid_reasons:
                blocking_reasons = [
                    r for r in invalid_reasons
                    if any(r.startswith(prefix) for prefix in BLOCKING_GUARD_PREFIXES)
                ]
                nonblocking_reasons = [
                    r for r in invalid_reasons if r not in blocking_reasons
                ]
                has_blocking = bool(blocking_reasons)
                LOGGER.warning(
                    "%s DNI=%s: invalid sheet sequence detected%s (%s)",
                    commission.nombre.strip(),
                    student.dni.strip(),
                    ", blocking allocation" if has_blocking else ", continuing (non-blocking)",
                    "; ".join(invalid_reasons),
                )
                base_context = {
                    "commission": commission.nombre.strip(),
                    "dni": student.dni.strip(),
                    "pricing_inscripcion": str(commission.valor_inscripcion_promocion or commission.valor_inscripcion or ""),
                    "pricing_cuota": str(commission.valor_cuota_bonificada or commission.valor_cuota or ""),
                    "cantidad_cuotas": commission.cantidad_cuotas,
                }
                # Non-blocking reasons always go to LIMPIEZA_HOJA
                if nonblocking_reasons:
                    cleanup_context = {
                        **base_context,
                        "reasons": nonblocking_reasons,
                        "blocking": False,
                    }
                    self._record_cleanup_tasks("guard:invalid_sequence", cleanup_context)
                # Blocking reasons go to REVISIONES and halt allocation
                if has_blocking:
                    blocking_context = {
                        **base_context,
                        "reasons": blocking_reasons,
                        "blocking": True,
                    }
                    self._counters.pending_review += 1
                    self.context.save_pending_review(
                        run_id=self._run_id,
                        discrepancy_id=None,
                        reason="guard:invalid_sequence",
                        context_json=blocking_context,
                    )
                    return [], []

        # --- Evaluate stale reviews (after guard detection, before allocation) ---
        # Determine current guard state: when force_reprocess is True, guard
        # detection above was skipped, so re-detect now for stale evaluation.
        if force_reprocess:
            current_guard_reasons = self._detect_invalid_sheet_sequence(commission, actual_rows)
        else:
            # invalid_reasons was computed above; it's [] when no guard fired.
            current_guard_reasons = invalid_reasons

        # Build current anomaly set from the student's rows
        student_anomalies: set[str] = set()
        for row in actual_rows:
            if row.tipo_movimiento == "Cobro" and row.medio_pago == "No aplica":
                student_anomalies.add("cobro_no_aplica")
            if row.tipo_movimiento == "Venta" and row.id_movimiento_bancario is not None:
                student_anomalies.add("venta_with_movement")
            if row.tipo_movimiento == "Cobro" and not (row.medio_pago or "").strip():
                student_anomalies.add("missing_medio")
            if row.monto <= 0:
                student_anomalies.add("negative_monto")

        self._evaluate_stale_reviews(
            commission=commission,
            student_dni=student.dni,
            actual_rows=actual_rows,
            current_guard_reasons=current_guard_reasons,
            current_anomalies=student_anomalies,
        )

        # --- Exclude payments already assigned to OTHER commissions ---
        # If this student is in multiple commissions, a payment that is
        # already reflected in the sheet for another commission should not
        # be processed again here.  This prevents the same payment from
        # being allocated to two different commissions.
        other_commission_pago_ids: set[int] = set()
        for row in sheet_rows:
            if row.dni.strip() != student.dni.strip():
                continue
            if (row.comision or "").strip() == commission.nombre.strip():
                continue  # same commission — not "other"
            if row.id_pago_mp is not None and row.id_pago_mp > 0:
                other_commission_pago_ids.add(row.id_pago_mp)

        if other_commission_pago_ids:
            before_count = len(enriched_conciliated)
            enriched_conciliated = [
                cp for cp in enriched_conciliated
                if cp.payment.id_pago_mp not in other_commission_pago_ids
            ]
            excluded = before_count - len(enriched_conciliated)
            if excluded:
                LOGGER.info(
                    "%s DNI=%s: excluded %d payments already assigned to other commissions",
                    commission.nombre.strip(),
                    student.dni.strip(),
                    excluded,
                )

        # --- Skip payments already reflected in the sheet ---
        # Strategy: find the most recent payment that has a Cobro with its
        # Any real id_pago_mp already present in the student's sheet rows
        # protects that payment from re-allocation. This avoids re-touching
        # payments that the sheet already linked explicitly, even if some rows
        # around them were loaded manually or by older agent behavior.
        # Use --force-reprocess to override this and process everything.
        protected_pago_ids: set[int] = set()
        ledger_seed_rows: list[SheetRow] = []
        if not force_reprocess:
            # Only trust ids that correspond to real payments for this student;
            # legacy/manual rows may carry stray values in the id columns.
            real_payment_ids = {cp.payment.id_pago_mp for cp in enriched_conciliated}
            protected_pago_ids = self._collect_protected_payment_ids(actual_rows, real_payment_ids)

            if protected_pago_ids:
                before_count = len(enriched_conciliated)
                enriched_conciliated = [
                    cp for cp in enriched_conciliated
                    if cp.payment.id_pago_mp not in protected_pago_ids
                ]
                skipped_protected = before_count - len(enriched_conciliated)
                ledger_seed_rows = [
                    row
                    for row in actual_rows
                    if row.tipo_movimiento.strip().casefold() == "venta"
                ]
                if skipped_protected:
                    LOGGER.info(
                        "%s DNI=%s: protected %d payments already linked by id_pago_mp",
                        commission.nombre.strip(),
                        student.dni.strip(),
                        skipped_protected,
                    )

        # Protected payment ids seed the ledger and stay out of reconciliation.
        # Manual rows (id_pago_mp=None) pass through naturally since
        # None is never in protected_pago_ids (a set[int]).
        use_sheet_ledger = not force_reprocess and bool(ledger_seed_rows)
        if protected_pago_ids:
            actual_rows_for_reconciler = [
                row for row in actual_rows
                if row.id_pago_mp not in protected_pago_ids
            ]
        else:
            actual_rows_for_reconciler = actual_rows

        allocation_result = allocator.allocate(
            payments=enriched_conciliated,
            existing_sheet_rows=actual_rows,
            student=student,
            seed_ledger_from_sheet=use_sheet_ledger,
            ledger_seed_rows=ledger_seed_rows if use_sheet_ledger else None,
        )

        student_active_commissions = self.sql.get_active_commissions_for_student(
            student.id_persona,
            year=payment_year,
            id_organizacion=commission.id_organizacion,
        )

        resolved_allocations = list(allocation_result.allocated)
        unresolved_ambiguous: list[AmbiguousPayment] = []
        for ambiguous in allocation_result.ambiguous:
            resolved = self._resolve_ambiguous(
                ambiguous,
                student,
                commission,
                all_payments,
                student_active_commissions,
                sheet_rows=actual_rows,
                current_guard_reasons=current_guard_reasons,
            )
            if resolved:
                resolved_allocations.extend(resolved)
            else:
                unresolved_ambiguous.append(ambiguous)

        # --- Pass 2: rebuild cuota ordinals ---
        # After all ambiguous payments are resolved, renumber cuotas
        # chronologically with the correct ledger state.
        # When settled payment IDs were skipped, seed the renumbering ledger
        # from those settled rows so cuota ordinals continue from the
        # pre-existing state instead of restarting at 1.
        # next_venta is intentionally discarded — the pipeline only inserts
        # rows backed by real payments, never placeholder "next to pay" rows.
        sheet_ledger = Ledger.from_sheet_rows(ledger_seed_rows) if use_sheet_ledger else None
        resolved_allocations, _next_venta = allocator.renumber_allocations(
            resolved_allocations, student, initial_ledger=sheet_ledger,
        )

        discrepancies = self.reconciler.reconcile(
            allocations=resolved_allocations,
            sheet_rows=actual_rows_for_reconciler,
            next_venta=None,
            commission_name=commission.nombre,
        )
        llm_resolutions = self._classify_and_resolve(
            discrepancies,
            student,
            commission,
            all_payments,
            sheet_rows=actual_rows_for_reconciler,
            ledger_rows=actual_rows,
            current_guard_reasons=current_guard_reasons,
        )

        # NOTE: _delete_excess_rows is intentionally skipped.  The pipeline
        # operates in insert-only mode — existing sheet rows (including
        # legacy/MAKE entries) are never deleted or modified.  Duplicate
        # cleanup is a separate manual/supervised process.

        # NOTE: unresolved ambiguous reviews are already persisted inside
        # _resolve_ambiguous() with full LLM decision context.  Do NOT
        # create a second review here — that was the source of duplicate
        # "ambiguous_allocation:*" + "unresolved_ambiguous_payment" rows.

        self.context.save_checkpoint(
            run_id=self._run_id,
            phase="student",
            commission_name=commission.nombre,
            student_dni=student.dni,
            checkpoint_data={
                "discrepancies": len(discrepancies),
                "resolved": len(llm_resolutions),
                "unresolved_ambiguous": len(unresolved_ambiguous),
            },
            status="done",
        )
        return discrepancies, llm_resolutions

    def _build_expected_rows_from_allocations(
        self,
        allocations: list[Allocation],
        commission: Commission,
        student: Student,
    ) -> list[ExpectedRow]:
        rows: list[ExpectedRow] = []
        student_name = f"{student.apellidos},{student.nombres}"
        estado_admin = map_estado_administrativo(student.id_estado_administrativo)

        for alloc in allocations:
            payment = alloc.payment.payment
            movement = alloc.payment.movement

            if alloc.generates_venta:
                rows.append(
                    ExpectedRow(
                        comision=commission.nombre,
                        fecha_movimiento=payment.fecha.date(),
                        tipo_movimiento="Venta",
                        dni=student.dni,
                        concepto=alloc.concept,
                        monto=Decimal(alloc.amount),
                        medio_pago="No aplica",
                        estudiante=student_name,
                        estado_administrativo=estado_admin,
                        id_movimiento_bancario=None,
                        id_pago_mp=payment.id_pago_mp,
                        source_payment=payment,
                        source_movement=movement,
                    )
                )

            if alloc.generates_cobro and movement is not None:
                rows.append(
                    ExpectedRow(
                        comision=commission.nombre,
                        fecha_movimiento=movement.fecha,
                        tipo_movimiento="Cobro",
                        dni=student.dni,
                        concepto=alloc.concept,
                        monto=Decimal(alloc.amount),
                        medio_pago=map_cobro_medio(payment.id_medio_pago or 0, has_bank_movement=movement is not None),
                        estudiante=student_name,
                        estado_administrativo=estado_admin,
                        id_movimiento_bancario=movement.id_movimiento,
                        id_pago_mp=payment.id_pago_mp,
                        source_payment=payment,
                        source_movement=movement,
                    )
                )

        return rows

    @staticmethod
    def _rows_match_exactly(
        expected: list[ExpectedRow],
        actual: list[SheetRow],
    ) -> bool:
        """Check if sheet rows already reflect the expected state perfectly.

        Compares by (tipo_movimiento, concepto, monto) ignoring order.
        If counts and content match, no rewrite is needed.
        """
        if len(expected) != len(actual):
            return False

        def _monto_str(monto: object) -> str:
            from decimal import Decimal as _Decimal

            try:
                value = monto if isinstance(monto, _Decimal) else _Decimal(str(monto))
            except Exception:
                return str(monto).strip()
            return format(value.normalize(), "f")

        def _sig(tipo: str, concepto: str, monto: object) -> tuple[str, str, str]:
            return (
                tipo.strip().casefold(),
                concepto.strip().casefold(),
                _monto_str(monto),
            )

        expected_sigs = sorted(
            _sig(e.tipo_movimiento, e.concepto, e.monto) for e in expected
        )
        actual_sigs = sorted(
            _sig(a.tipo_movimiento, a.concepto, a.monto) for a in actual
        )
        return expected_sigs == actual_sigs

    def _delete_excess_rows(
        self,
        expected_rows: list[ExpectedRow],
        actual_rows: list[SheetRow],
        discrepancies: list[Discrepancy],
    ) -> None:
        """Delete sheet rows that exceed the expected count per (tipo, concepto, monto).

        After reconciliation, the sheet may still have duplicates from prior runs
        that the reconciler consumed via weak match. This counts how many of each
        signature SHOULD exist vs how many DO exist, and deletes the excess.

        When a row is already matched to an expected row but still needs field
        corrections, count it using the expected signature instead of the raw
        current values. Otherwise the same row can be scheduled for both update
        and delete in a single pass.
        """
        from collections import Counter

        def _monto_key(monto: object) -> str:
            try:
                return format(Decimal(str(monto)).normalize(), "f")
            except Exception:
                return str(monto)

        expected_counts: Counter[tuple[str, str, str]] = Counter()
        for e in expected_rows:
            key = (e.tipo_movimiento.strip().casefold(), e.concepto.strip().casefold(), _monto_key(e.monto))
            expected_counts[key] += 1

        expected_sig_by_row: dict[int, tuple[str, str, str]] = {}
        for disc in discrepancies:
            if disc.actual_row is None or disc.expected_row is None:
                continue
            if disc.discrepancy_type == DiscrepancyType.EXTRA_ROW:
                continue
            expected_sig_by_row[disc.actual_row.row_number] = (
                disc.expected_row.tipo_movimiento.strip().casefold(),
                disc.expected_row.concepto.strip().casefold(),
                _monto_key(disc.expected_row.monto),
            )

        # Group actual rows by signature, preserving row order
        actual_by_sig: dict[tuple[str, str, str], list[SheetRow]] = {}
        for row in actual_rows:
            key = expected_sig_by_row.get(
                row.row_number,
                (row.tipo_movimiento.strip().casefold(), row.concepto.strip().casefold(), _monto_key(row.monto)),
            )
            actual_by_sig.setdefault(key, []).append(row)

        for sig, rows_list in actual_by_sig.items():
            allowed = expected_counts.get(sig, 0)
            if len(rows_list) > allowed:
                excess = rows_list[allowed:]  # keep the first N, delete the rest
                for row in excess:
                    self.patch_builder.add_delete(
                        row=row,
                        discrepancy_id=f"dedup-{row.row_number}",
                    )
                    self._counters.auto_fix += 1
                    self._counters.discrepancies_total += 1

    def _clean_orphaned_rows(
        self,
        commission: Commission,
        active_students: list[Student],
        sheet_rows: list[SheetRow],
    ) -> None:
        """Blank out sheet rows for students no longer in this commission.

        If a student was transferred or removed, their rows in the sheet
        for this commission should be cleaned up to avoid stale data.
        """
        active_dnis = {s.dni.strip() for s in active_students}
        commission_name = commission.nombre.strip()

        for row in sheet_rows:
            if not row.comision or row.comision.strip() != commission_name:
                continue
            row_dni = (row.dni or "").strip()
            if not row_dni or row_dni in active_dnis:
                continue
            # This row belongs to someone no longer in the commission
            LOGGER.info(
                "Orphaned row %s: DNI %s not in commission %s",
                row.row_number, row_dni, commission_name,
            )
            self.patch_builder.add_delete(
                row=row,
                discrepancy_id=f"orphan-{row.row_number}",
            )

    @staticmethod
    def _ensure_student_dni(conciliated_payment, student_dni: str):
        payment = conciliated_payment.payment
        updated_payment = payment.model_copy(update={"dni_cuit_originante": student_dni})
        return conciliated_payment.model_copy(update={"payment": updated_payment})

    @staticmethod
    def _extract_cuota_number(concepto: str | None) -> int | None:
        if not concepto:
            return None
        match = re.search(r"Cuota\s+(\d+)", concepto, re.IGNORECASE)
        return int(match.group(1)) if match else None

    @staticmethod
    def _collect_protected_payment_ids(actual_rows: list[SheetRow], real_payment_ids: set[int]) -> set[int]:
        """Return real payment ids already linked in the student's sheet rows."""
        protected_pago_ids: set[int] = set()
        for row in actual_rows:
            if row.id_pago_mp is None or row.id_pago_mp <= 0:
                continue
            if row.id_pago_mp not in real_payment_ids:
                continue
            protected_pago_ids.add(row.id_pago_mp)
        return protected_pago_ids

    @staticmethod
    def _is_close_amount(actual: Decimal, target: Decimal, tolerance: Decimal = Decimal("0.02")) -> bool:
        if target <= 0:
            return False
        return abs(actual - target) / target <= tolerance

    @staticmethod
    def _commission_has_short_course_single_payment(commission: Commission) -> bool:
        total_cuotas = commission.cantidad_cuotas or 0
        duration_months = commission.duracion_meses or 0
        cuota = commission.valor_cuota_bonificada or commission.valor_cuota
        return (cuota is None or cuota <= 0) and total_cuotas == 0 and 0 < duration_months < 9

    def _detect_invalid_sheet_sequence(
        self,
        commission: Commission,
        actual_rows: list[SheetRow],
    ) -> list[str]:
        venta_rows = [row for row in actual_rows if (row.tipo_movimiento or "").strip().casefold() == "venta"]
        if not venta_rows:
            return []

        reasons: list[str] = []
        cuota_numbers: list[int] = []
        has_inscription = False
        total_cuotas = commission.cantidad_cuotas or 0
        is_short_single = self._commission_has_short_course_single_payment(commission)

        cuota_counts: dict[int, int] = {}
        for row in venta_rows:
            concepto = (row.concepto or "").strip()
            concepto_lower = concepto.casefold()
            if "inscripción" in concepto_lower or "inscripcion" in concepto_lower:
                has_inscription = True
                continue

            cuota_n = self._extract_cuota_number(concepto)
            if cuota_n is None:
                continue

            cuota_numbers.append(cuota_n)
            cuota_counts[cuota_n] = cuota_counts.get(cuota_n, 0) + 1

        if cuota_numbers and not has_inscription and not is_short_single:
            reasons.append("missing_inscription_with_existing_cuotas")

        for cuota_n, count in cuota_counts.items():
            if count > 1:
                reasons.append(f"duplicate_cuota_{cuota_n}")

        if cuota_numbers:
            max_cuota = max(cuota_numbers)
            missing = [n for n in range(1, max_cuota + 1) if n not in cuota_counts]
            if missing:
                reasons.append(f"missing_cuotas_before_{max_cuota}:{','.join(str(n) for n in missing)}")
            if total_cuotas > 0 and max_cuota > total_cuotas:
                reasons.append(f"cuota_exceeds_total:{max_cuota}>{total_cuotas}")

        deduped: list[str] = []
        seen: set[str] = set()
        for reason in reasons:
            if reason in seen:
                continue
            seen.add(reason)
            deduped.append(reason)
        return deduped

    def _resolve_ambiguous(
        self,
        ambiguous: AmbiguousPayment,
        student: Student,
        commission: Commission,
        payments: list[Payment],
        student_active_commissions: list[Commission],
        sheet_rows: list[SheetRow] | None = None,
        current_guard_reasons: list[str] | None = None,
    ) -> list[Allocation]:
        """Send ambiguous payment to LLM for resolution.

        Build context with candidates, payment history, commission prices.
        LLM picks the best candidate or flags for review.
        Returns a list of Allocations (may be >1 for combined payments like
        "Inscripción + Cuota 1"), or an empty list if unresolved.
        """
        assert self._run_id is not None

        # --- Deterministic: single candidate with exact monto match ---
        if len(ambiguous.candidates) == 1:
            only = ambiguous.candidates[0]
            payment_monto = ambiguous.payment.payment.monto
            if (
                payment_monto is not None
                and Decimal(str(only.amount)) == Decimal(str(payment_monto))
                and self._is_valid_allocation_concept(only.concept)
                and self._is_monto_plausible_for_concept(payment_monto, only.concept, commission)
            ):
                self._counters.auto_fix += 1
                return [Allocation(
                    payment=ambiguous.payment,
                    concept=only.concept,
                    amount=Decimal(only.amount),
                    generates_venta=True,
                    generates_cobro=ambiguous.payment.movement is not None,
                )]

        discrepancy = Discrepancy(
            id=f"amb-{next(self._discrepancy_ids)}",
            commission=commission.nombre,
            dni=student.dni,
            discrepancy_type=DiscrepancyType.WRONG_VALUE,
            field="concepto",
            expected_value=None,
            actual_value=None,
            expected_row=None,
            actual_row=None,
            confidence=0.0,
            severity=Severity.WARNING,
            resolution=None,
            resolved_by=None,
        )

        llm_context = self._build_context_for_llm(
            discrepancy,
            student,
            commission,
            payments,
            student_active_commissions,
            sheet_rows=sheet_rows,
            ledger_rows=sheet_rows,
            guard_reasons=current_guard_reasons,
            allocator_diagnostics=self._build_allocator_diagnostics(
                ambiguous,
                commission,
                sheet_rows or [],
                current_guard_reasons,
            ),
        )
        llm_context["ambiguous_payment"] = {
            "payment": ambiguous.payment.model_dump(mode="json"),
            "candidates": [candidate.model_dump(mode="json") for candidate in ambiguous.candidates],
        }

        decision = self.decision_engine.decide(discrepancy, llm_context)
        self._counters.llm_decided += 1

        if decision.action == "fix" and decision.confidence >= self.auto_threshold:
            # --- Try compound concept split (e.g. "Inscripción + Cuota 1") ---
            suggested = (decision.suggested_value or "").strip()
            compound = self._try_split_compound_allocation(
                suggested, ambiguous.payment, commission,
            )
            if compound:
                return compound

            # --- Single concept resolution ---
            # Prefer chosen_candidate_index (structured) over text matching
            chosen = None
            if (
                decision.chosen_candidate_index is not None
                and 0 <= decision.chosen_candidate_index < len(ambiguous.candidates)
            ):
                chosen = ambiguous.candidates[decision.chosen_candidate_index]

            # Fallback: match by suggested_value text
            if chosen is None:
                chosen = next(
                    (candidate for candidate in ambiguous.candidates if candidate.concept.casefold() == suggested.casefold()),
                    None,
                )

            # Last resort: pick highest-scoring candidate
            if chosen is None and ambiguous.candidates:
                chosen = max(ambiguous.candidates, key=lambda candidate: candidate.score)

            if chosen is not None:
                if not self._is_valid_allocation_concept(chosen.concept):
                    chosen = None

            # --- LLM suggested a valid concept not in candidates ---
            # When the allocator only generated "Desconocido" but the LLM
            # identified a canonical concept (e.g. "Cuota 1", "Inscripción"),
            # trust the LLM suggestion directly instead of discarding it.
            if chosen is None and self._is_valid_allocation_concept(suggested):
                monto = ambiguous.payment.payment.monto
                llm_tolerance = Decimal("0.60")
                if self._is_monto_plausible_for_concept(monto, suggested, commission, tolerance=llm_tolerance):
                    self._counters.auto_fix += 1
                    return [Allocation(
                        payment=ambiguous.payment,
                        concept=suggested,
                        amount=monto,
                        generates_venta=True,
                        generates_cobro=ambiguous.payment.movement is not None,
                    )]

            # Block allocations where the monto is wildly different from any
            # known commission price — prevents $274.000 being labelled as
            # "Inscripción" when inscription costs $54.800 or $109.600.
            # Use relaxed tolerance (60%) for high-confidence LLM decisions
            # to accommodate scholarships/discounts (e.g. 50% beca).
            if chosen is not None:
                monto = ambiguous.payment.payment.monto
                llm_tolerance = Decimal("0.60") if decision.confidence >= self.auto_threshold else None
                if not self._is_monto_plausible_for_concept(monto, chosen.concept, commission, tolerance=llm_tolerance):
                    chosen = None

            if chosen is not None:
                return [Allocation(
                    payment=ambiguous.payment,
                    concept=chosen.concept,
                    amount=Decimal(chosen.amount),
                    generates_venta=True,
                    generates_cobro=ambiguous.payment.movement is not None,
                )]

        self._counters.pending_review += 1
        commission_prices = {
            "inscripcion": str(commission.valor_inscripcion_promocion or commission.valor_inscripcion or ""),
            "cuota": str(commission.valor_cuota_bonificada or commission.valor_cuota or ""),
            "cantidad_cuotas": commission.cantidad_cuotas,
            "pago_unico": str(commission.valor_pago_unico) if commission.valor_pago_unico else None,
        }
        self.context.save_pending_review(
            run_id=self._run_id,
            discrepancy_id=None,
            reason=f"ambiguous_allocation:{decision.action}",
            context_json={
                "commission": commission.nombre.strip(),
                "dni": student.dni.strip(),
                "payment_id": ambiguous.payment.payment.id_pago_mp,
                "monto": str(ambiguous.payment.payment.monto),
                "fecha": ambiguous.payment.payment.fecha.date().isoformat() if ambiguous.payment.payment.fecha else None,
                "decision": decision.model_dump(mode="json"),
                "candidates": [candidate.model_dump(mode="json") for candidate in ambiguous.candidates],
                "commission_prices": commission_prices,
            },
        )
        return []

    def _classify_and_resolve(
        self,
        discrepancies: list[Discrepancy],
        student: Student,
        commission: Commission,
        payments: list[Payment],
        sheet_rows: list[SheetRow] | None = None,
        ledger_rows: list[SheetRow] | None = None,
        current_guard_reasons: list[str] | None = None,
    ) -> list[Any]:
        resolved: list[Any] = []
        assert self._run_id is not None

        for discrepancy in discrepancies:
            if self._is_direct_autofix_missing_row(discrepancy):
                discrepancy.confidence = 1.0
                discrepancy.severity = Severity.CRITICAL
                discrepancy.resolution = Resolution.AUTO_FIX
                discrepancy.resolved_by = "rules"
                self._counters.discrepancies_total += 1
                self._counters.auto_fix += 1
                self._plan_autofix(discrepancy)
                self.context.save_discrepancy(self._run_id, discrepancy)
                continue

            if self._is_missing_row_without_context(discrepancy):
                discrepancy.confidence = 0.0
                discrepancy.severity = Severity.WARNING
                discrepancy.resolution = Resolution.PENDING_REVIEW
                discrepancy.resolved_by = "missing_row_without_context"
                self._counters.discrepancies_total += 1
                self._counters.pending_review += 1
                self._plan_review(discrepancy, "Missing row without enough context for automatic fix")
                db_disc_id = self.context.save_discrepancy(self._run_id, discrepancy)
                self.context.save_pending_review(
                    run_id=self._run_id,
                    discrepancy_id=db_disc_id,
                    reason=discrepancy.resolved_by,
                    context_json={
                        "commission": discrepancy.commission,
                        "dni": discrepancy.dni,
                        "type": discrepancy.discrepancy_type.value,
                    },
                )
                continue

            # Extra rows (legacy/MAKE entries not matched to any expected row)
            # are intentionally left untouched.  The pipeline operates in
            # insert-only mode and never deletes existing sheet data.
            if discrepancy.discrepancy_type == DiscrepancyType.EXTRA_ROW:
                discrepancy.confidence = 0.0
                discrepancy.severity = Severity.INFO
                discrepancy.resolution = Resolution.SKIPPED
                discrepancy.resolved_by = "insert_only_policy"
                self._counters.discrepancies_total += 1
                self.context.save_discrepancy(self._run_id, discrepancy)
                continue

            # --- Deterministic medio_pago fix ---
            # If the sheet says "Mercado Pago" but the expected row has a bank
            # movement, the correct medio is "Transferencia Bancaria".  No need
            # to send this to LLM or human review.
            if (
                discrepancy.discrepancy_type == DiscrepancyType.WRONG_VALUE
                and discrepancy.field == "medio_pago"
                and discrepancy.expected_row is not None
                and discrepancy.expected_row.source_movement is not None
                and str(discrepancy.expected_value or "").strip().casefold() == "transferencia bancaria"
            ):
                discrepancy.confidence = 1.0
                discrepancy.severity = Severity.INFO
                discrepancy.resolution = Resolution.AUTO_FIX
                discrepancy.resolved_by = "medio_pago_deterministic"
                self._counters.discrepancies_total += 1
                self._counters.auto_fix += 1
                self._plan_autofix(discrepancy)
                self.context.save_discrepancy(self._run_id, discrepancy)
                continue

            if discrepancy.resolved_by == "split_detection":
                # Split detection already set high confidence — don't override
                pass
            else:
                commission_pricing = {
                    "inscripcion": commission.valor_inscripcion_promocion or Decimal("0"),
                    "cuota": commission.valor_cuota_bonificada or Decimal("0"),
                    "cantidad_cuotas": Decimal(str(commission.cantidad_cuotas or 0)),
                }
                discrepancy.confidence = self.scorer.score(discrepancy, commission_pricing)
            discrepancy.severity = self.scorer.assign_severity(discrepancy)
            self._counters.discrepancies_total += 1

            if discrepancy.confidence >= self.auto_threshold:
                discrepancy.resolution = Resolution.AUTO_FIX
                discrepancy.resolved_by = "rules"
                self._counters.auto_fix += 1
                self._plan_autofix(discrepancy)
            elif discrepancy.confidence >= self.llm_threshold:
                llm_context = self._build_context_for_llm(
                    discrepancy,
                    student,
                    commission,
                    payments,
                    sheet_rows=sheet_rows,
                    ledger_rows=ledger_rows,
                    guard_reasons=current_guard_reasons,
                )
                decision = self.decision_engine.decide(discrepancy, llm_context)
                if decision.action == "fix":
                    discrepancy.resolution = Resolution.LLM_DECIDED
                    discrepancy.resolved_by = decision.model_used
                    self._apply_llm_fix(discrepancy, decision.suggested_value)
                elif decision.action == "flag_review":
                    discrepancy.resolution = Resolution.PENDING_REVIEW
                    discrepancy.resolved_by = decision.model_used
                    self._plan_review(discrepancy, decision.reasoning)
                else:
                    discrepancy.resolution = Resolution.SKIPPED
                    discrepancy.resolved_by = decision.model_used
                self._counters.llm_decided += 1
                resolved.append(decision)
            elif self._should_escalate_to_llm(discrepancy):
                llm_context = self._build_context_for_llm(
                    discrepancy, student, commission, payments,
                    sheet_rows=sheet_rows,
                    ledger_rows=ledger_rows,
                    guard_reasons=current_guard_reasons,
                )
                decision = self.decision_engine.decide(discrepancy, llm_context)
                if decision.action == "fix":
                    discrepancy.resolution = Resolution.LLM_DECIDED
                    discrepancy.resolved_by = decision.model_used
                    self._apply_llm_fix(discrepancy, decision.suggested_value)
                elif decision.action == "flag_review":
                    discrepancy.resolution = Resolution.PENDING_REVIEW
                    discrepancy.resolved_by = decision.model_used
                    self._plan_review(discrepancy, decision.reasoning)
                else:
                    discrepancy.resolution = Resolution.SKIPPED
                    discrepancy.resolved_by = decision.model_used
                self._counters.llm_decided += 1
                resolved.append(decision)
            else:
                discrepancy.resolution = Resolution.PENDING_REVIEW
                discrepancy.resolved_by = "scorer"
                self._plan_review(discrepancy, "Low confidence score")
                self._counters.pending_review += 1

            db_disc_id = self.context.save_discrepancy(self._run_id, discrepancy)
            if discrepancy.resolution == Resolution.PENDING_REVIEW:
                self.context.save_pending_review(
                    run_id=self._run_id,
                    discrepancy_id=db_disc_id,
                    reason=discrepancy.resolved_by or "pending_review",
                    context_json={
                        "commission": discrepancy.commission,
                        "dni": discrepancy.dni,
                        "type": discrepancy.discrepancy_type.value,
                        "field": discrepancy.field,
                        "expected_value": discrepancy.expected_value,
                        "actual_value": discrepancy.actual_value,
                        "row_number": discrepancy.actual_row.row_number if discrepancy.actual_row else None,
                        "concepto": discrepancy.expected_row.concepto if discrepancy.expected_row else (
                            discrepancy.actual_row.concepto if discrepancy.actual_row else None
                        ),
                        "monto": str(discrepancy.expected_row.monto) if discrepancy.expected_row else (
                            str(discrepancy.actual_row.monto) if discrepancy.actual_row else None
                        ),
                        "payment_id": discrepancy.expected_row.id_pago_mp if discrepancy.expected_row else (
                            discrepancy.actual_row.id_pago_mp if discrepancy.actual_row else None
                        ),
                    },
                )

        return resolved

    @staticmethod
    def _should_escalate_to_llm(discrepancy: Discrepancy) -> bool:
        """Decide if a low-confidence discrepancy should still go to the LLM.

        Wrong values in concepto, fecha, medio_pago, or monto often come from
        MAKE loading plan rows with different ordinals, generic dates, or
        bonified prices that differ from what the student actually paid.
        The LLM can resolve these by comparing the full student history
        against DB payment records and commission pricing.
        """
        if discrepancy.discrepancy_type != DiscrepancyType.WRONG_VALUE:
            return False
        return discrepancy.field in ("concepto", "fecha_movimiento", "medio_pago", "monto")

    def _build_context_for_llm(
        self,
        discrepancy: Discrepancy,
        student: Student,
        commission: Commission,
        payments: list[Payment],
        student_active_commissions: list[Commission] | None = None,
        sheet_rows: list[SheetRow] | None = None,
        ledger_rows: list[SheetRow] | None = None,
        guard_reasons: list[str] | None = None,
        allocator_diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payment_record = discrepancy.expected_row.source_payment if discrepancy.expected_row else None
        movement = discrepancy.expected_row.source_movement if discrepancy.expected_row else None
        derived_rows = ledger_rows if ledger_rows is not None else (sheet_rows or [])
        context: dict[str, Any] = {
            "payment_history": [
                {
                    "id_pago_mp": p.id_pago_mp,
                    "fecha": p.fecha.isoformat(),
                    "monto": str(p.monto),
                    "id_concepto_pago": p.id_concepto_pago,
                    "id_movimiento_bancario": p.id_movimiento_bancario,
                }
                for p in payments
            ],
            "payment_history_summary": self._build_payment_history_summary(payments),
            "commission_prices": {
                "inscripcion": str(commission.valor_inscripcion_promocion or ""),
                "cuota": str(commission.valor_cuota_bonificada or ""),
                "cantidad_cuotas": commission.cantidad_cuotas,
                "pago_unico": str(commission.valor_pago_unico) if commission.valor_pago_unico else None,
            },
            "student_info": {
                "id_persona": student.id_persona,
                "dni": student.dni,
                "estudiante": f"{student.apellidos} {student.nombres}",
                "persona_observaciones": student.persona_observaciones,
                "comision_observaciones": student.comision_observaciones,
                "fecha_hora_inscripcion": student.fecha_hora_inscripcion.isoformat() if student.fecha_hora_inscripcion else None,
            },
            "active_commissions": [
                {
                    "id_comision": c.id_comision,
                    "nombre": c.nombre.strip(),
                    "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
                    "inscripcion": str(c.valor_inscripcion_promocion or ""),
                    "cuota": str(c.valor_cuota_bonificada or ""),
                    "cantidad_cuotas": c.cantidad_cuotas,
                }
                for c in (student_active_commissions or [])
            ],
            "payment_record": payment_record.model_dump(mode="json") if payment_record else None,
            "bank_movement": movement.model_dump(mode="json") if movement else None,
            "ledger_summary": self._build_ledger_summary(commission, derived_rows),
            "sequence_integrity": self._build_sequence_integrity(commission, derived_rows, guard_reasons),
            "allocator_diagnostics": allocator_diagnostics or {},
        }
        if sheet_rows is not None:
            context["existing_sheet_rows"] = [
                {
                    "row_number": r.row_number,
                    "tipo_movimiento": r.tipo_movimiento,
                    "concepto": r.concepto,
                    "monto": str(r.monto),
                    "fecha": r.fecha_movimiento.isoformat() if r.fecha_movimiento else None,
                    "medio_pago": r.medio_pago,
                    "id_pago_mp": r.id_pago_mp,
                    "id_movimiento_bancario": r.id_movimiento_bancario,
                }
                for r in sheet_rows
            ]
        return context

    @staticmethod
    def _build_payment_history_summary(payments: list[Payment]) -> dict[str, Any]:
        if not payments:
            return {
                "total_payments": 0,
                "conciliated_payments": 0,
                "unconciliated_payments": 0,
                "uncontrolled_payments": 0,
                "total_reported_amount": "0",
                "first_payment_date": None,
                "last_payment_date": None,
                "db_concept_counts": {"inscripcion": 0, "cuota": 0, "other": 0},
            }

        payment_dates = [payment.fecha for payment in payments if payment.fecha is not None]
        concept_counts = {"inscripcion": 0, "cuota": 0, "other": 0}
        conciliated = 0
        uncontrolled = 0
        total_amount = Decimal("0")

        for payment in payments:
            total_amount += payment.monto or Decimal("0")
            if payment.id_movimiento_bancario is not None and payment.id_movimiento_bancario > 0:
                conciliated += 1
            if not payment.controlado:
                uncontrolled += 1

            if payment.id_concepto_pago == 1:
                concept_counts["inscripcion"] += 1
            elif payment.id_concepto_pago in (2, 4):
                concept_counts["cuota"] += 1
            else:
                concept_counts["other"] += 1

        return {
            "total_payments": len(payments),
            "conciliated_payments": conciliated,
            "unconciliated_payments": len(payments) - conciliated,
            "uncontrolled_payments": uncontrolled,
            "total_reported_amount": str(total_amount),
            "first_payment_date": min(payment_dates).isoformat() if payment_dates else None,
            "last_payment_date": max(payment_dates).isoformat() if payment_dates else None,
            "db_concept_counts": concept_counts,
        }

    def _build_ledger_summary(self, commission: Commission, rows: list[SheetRow]) -> dict[str, Any]:
        ledger = Ledger.from_sheet_rows(rows)
        protected_payment_ids = sorted(
            {
                row.id_pago_mp
                for row in rows
                if row.id_pago_mp is not None and row.id_pago_mp > 0
            }
        )
        cuota_numbers = sorted(
            {
                cuota_n
                for row in rows
                if (row.tipo_movimiento or "").strip().casefold() == "venta"
                for cuota_n in [self._extract_cuota_number(row.concepto)]
                if cuota_n is not None
            }
        )
        total_cuotas = commission.cantidad_cuotas or 0
        next_expected_cuota = None
        remaining_cuotas = None
        if total_cuotas > 0 and not ledger.pago_unico:
            remaining_cuotas = max(total_cuotas - ledger.cuotas_paid, 0)
            if remaining_cuotas > 0:
                next_expected_cuota = ledger.cuotas_paid + 1

        return {
            "inscription_paid": ledger.inscription_paid,
            "cuotas_paid": ledger.cuotas_paid,
            "pago_unico_present": ledger.pago_unico,
            "fully_paid": ledger.fully_paid,
            "existing_concepts": sorted(ledger.existing_concepts),
            "existing_cuota_numbers": cuota_numbers,
            "protected_payment_ids": protected_payment_ids,
            "protected_payment_count": len(protected_payment_ids),
            "next_expected_cuota": next_expected_cuota,
            "remaining_cuotas": remaining_cuotas,
            "source_row_count": len(rows),
            "venta_row_count": sum(1 for row in rows if (row.tipo_movimiento or "").strip().casefold() == "venta"),
            "cobro_row_count": sum(1 for row in rows if (row.tipo_movimiento or "").strip().casefold() == "cobro"),
        }

    def _build_sequence_integrity(
        self,
        commission: Commission,
        rows: list[SheetRow],
        guard_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        reasons = list(guard_reasons) if guard_reasons is not None else self._detect_invalid_sheet_sequence(commission, rows)
        duplicate_cuotas = sorted(
            int(reason.removeprefix("duplicate_cuota_"))
            for reason in reasons
            if reason.startswith("duplicate_cuota_")
        )
        gap_reasons = [reason for reason in reasons if reason.startswith("missing_cuotas_before_")]
        return {
            "trusted": not reasons,
            "guard_reasons": reasons,
            "has_duplicates": bool(duplicate_cuotas),
            "duplicate_cuotas": duplicate_cuotas,
            "has_gaps": bool(gap_reasons),
            "gap_reasons": gap_reasons,
            "missing_inscription": "missing_inscription_with_existing_cuotas" in reasons,
            "exceeds_total": any(reason.startswith("cuota_exceeds_total:") for reason in reasons),
        }

    def _build_allocator_diagnostics(
        self,
        ambiguous: AmbiguousPayment,
        commission: Commission,
        rows: list[SheetRow],
        guard_reasons: list[str] | None = None,
    ) -> dict[str, Any]:
        ledger_summary = self._build_ledger_summary(commission, rows)
        sequence_integrity = self._build_sequence_integrity(commission, rows, guard_reasons)
        highest_candidate = max(ambiguous.candidates, key=lambda candidate: candidate.score) if ambiguous.candidates else None
        return {
            "path": "ambiguous_allocation",
            "deterministic_allocator_failed": True,
            "candidate_count": len(ambiguous.candidates),
            "candidate_concepts": [candidate.concept for candidate in ambiguous.candidates],
            "highest_scoring_candidate": highest_candidate.model_dump(mode="json") if highest_candidate else None,
            "next_expected_cuota_from_ledger": ledger_summary.get("next_expected_cuota"),
            "sequence_trusted": sequence_integrity.get("trusted", True),
        }

    def _evaluate_stale_reviews(
        self,
        commission: Commission,
        student_dni: str,
        actual_rows: list[SheetRow],
        current_guard_reasons: list[str],
        current_anomalies: set[str],
    ) -> None:
        """Auto-close stale guard and anomaly reviews for this student.

        Called per-student inside _process_student() after guard and anomaly
        detection but before new review creation.
        """
        commission_name = commission.nombre.strip()
        dni = student_dni.strip()

        # --- Guard reviews ---
        guard_reviews = self.context.get_open_reviews_for_student(commission_name, dni)
        current_guard_set = set(current_guard_reasons)
        for review in guard_reviews:
            ctx = json.loads(review.get("context_json") or "{}")
            review_reasons = set(ctx.get("reasons", []))
            has_blocking_review_reason = any(
                any(reason.startswith(prefix) for prefix in BLOCKING_GUARD_PREFIXES)
                for reason in review_reasons
            )
            if not has_blocking_review_reason:
                self.context.close_review(review["id"], "auto_close:moved_to_cleanup")
                LOGGER.info(
                    "auto-closed non-blocking guard review id=%s for %s DNI=%s",
                    review["id"], commission_name, dni,
                )
                continue
            if not review_reasons & current_guard_set:
                self.context.close_review(review["id"], "auto_close:guard_resolved")
                LOGGER.info(
                    "auto-closed stale guard review id=%s for %s DNI=%s",
                    review["id"], commission_name, dni,
                )

        # --- Anomaly reviews ---
        student_row_numbers = [r.row_number for r in actual_rows]
        anomaly_reviews = self.context.get_open_anomaly_reviews_for_rows(student_row_numbers)
        for review in anomaly_reviews:
            reason = review.get("reason", "")
            anomaly_type = reason.split(":", 1)[1] if ":" in reason else reason
            if anomaly_type in current_anomalies:
                self.context.close_review(review["id"], "auto_close:moved_to_cleanup")
                LOGGER.info(
                    "auto-closed anomaly review moved to cleanup id=%s type=%s",
                    review["id"], anomaly_type,
                )
                continue
            if anomaly_type not in current_anomalies:
                self.context.close_review(review["id"], "auto_close:anomaly_resolved")
                LOGGER.info(
                    "auto-closed stale anomaly review id=%s type=%s",
                    review["id"], anomaly_type,
                )

    def _generate_summary(self, run_id: str | None) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "cobros_blocked": self._counters.cobros_blocked,
            "commissions_processed": self._counters.commissions_processed,
            "students_processed": self._counters.students_processed,
            "discrepancies_total": self._counters.discrepancies_total,
            "auto_fix": self._counters.auto_fix,
            "llm_decided": self._counters.llm_decided,
            "pending_review": self._counters.pending_review,
            "cleanup_tasks": self._counters.cleanup_tasks,
            "sheet_anomalies": self._counters.sheet_anomalies,
            "errors": self._counters.errors,
        }

    def _record_cleanup_tasks(self, reason: str, context_json: dict[str, Any]) -> None:
        for task in self.review_manager.build_cleanup_tasks(reason, context_json):
            self._cleanup_tasks[task["task_key"]] = task

    @staticmethod
    def _is_direct_autofix_missing_row(discrepancy: Discrepancy) -> bool:
        return (
            discrepancy.discrepancy_type == DiscrepancyType.MISSING_ROW
            and discrepancy.expected_row is not None
        )

    @staticmethod
    def _is_missing_row_without_context(discrepancy: Discrepancy) -> bool:
        return (
            discrepancy.discrepancy_type == DiscrepancyType.MISSING_ROW
            and discrepancy.expected_row is None
        )

    @staticmethod
    def _should_track_anomaly(
        anomaly: Any,
        sheet_rows: list[SheetRow],
        tracked_commissions: set[str],
    ) -> bool:
        if not tracked_commissions:
            return False
        row = next((r for r in sheet_rows if r.row_number == anomaly.row_number), None)
        if row is None or row.comision is None:
            return True
        return row.comision.strip() in tracked_commissions

    def _plan_autofix(self, discrepancy: Discrepancy) -> None:
        if discrepancy.discrepancy_type == DiscrepancyType.MISSING_ROW and discrepancy.expected_row is not None:
            self.patch_builder.add_insert(discrepancy.expected_row, discrepancy.id)
            return
        if discrepancy.discrepancy_type == DiscrepancyType.WRONG_VALUE and discrepancy.actual_row is not None:
            column = self._field_to_column(discrepancy.field)
            if column:
                self.patch_builder.add_update(
                    row_number=discrepancy.actual_row.row_number,
                    column=column,
                    old_value=discrepancy.actual_value or "",
                    new_value=discrepancy.expected_value or "",
                    discrepancy_id=discrepancy.id,
                )

    def _apply_llm_fix(self, discrepancy: Discrepancy, suggested_value: str | None) -> None:
        if discrepancy.discrepancy_type == DiscrepancyType.MISSING_ROW and discrepancy.expected_row is not None:
            self.patch_builder.add_insert(discrepancy.expected_row, discrepancy.id)
            return
        if discrepancy.actual_row is None:
            return
        column = self._field_to_column(discrepancy.field)
        if not column:
            return
        # For reconciliation discrepancies, the LLM decides WHETHER to apply the
        # fix, not WHAT value to invent. The canonical value is always the
        # `expected_value` computed from DB + rules. Using raw `suggested_value`
        # is dangerous because the model can describe a combined concept in a
        # monto field (e.g. "Inscripción + Cuota 1").
        final_value = discrepancy.expected_value or ""
        if discrepancy.field == "concepto" and not self._is_valid_allocation_concept(final_value):
            return
        if discrepancy.field == "concepto" and final_value:
            final_value = self._sanitize_concepto(final_value, discrepancy.expected_value)
        self.patch_builder.add_update(
            row_number=discrepancy.actual_row.row_number,
            column=column,
            old_value=discrepancy.actual_value or "",
            new_value=final_value,
            discrepancy_id=discrepancy.id,
        )

    @staticmethod
    def _try_split_compound_allocation(
        suggested: str,
        payment: ConciliatedPayment,
        commission: Commission,
    ) -> list[Allocation]:
        """Parse compound concepts like "Inscripción + Cuota 1" into multiple Allocations.

        Tries all combinations of known prices (bonified, full, and half-beca)
        for each part.  Returns a list of Allocations if the split is valid
        and amounts match the payment monto.  Returns an empty list otherwise
        (caller should fall through to single-concept logic).
        """
        if "+" not in suggested:
            return []

        parts = [p.strip() for p in suggested.split("+")]
        if len(parts) < 2:
            return []

        monto = payment.payment.monto
        if monto is None:
            return []

        # Build candidate prices for each part
        part_info: list[tuple[str, list[Decimal]]] = []
        for part in parts:
            lower = part.casefold()

            # Normalize concept name
            concept = part
            cuota_match = re.search(r"[Cc]uota\s*(\d+)", part)
            if cuota_match:
                concept = f"Cuota {cuota_match.group(1)}"
            elif "inscripción" in lower or "inscripcion" in lower:
                concept = "Inscripción"

            prices: list[Decimal] = []
            if "inscripción" in lower or "inscripcion" in lower:
                if commission.valor_inscripcion_promocion:
                    prices.append(commission.valor_inscripcion_promocion)
                if commission.valor_inscripcion:
                    prices.append(commission.valor_inscripcion)
            elif "cuota" in lower:
                if commission.valor_cuota_bonificada:
                    prices.append(commission.valor_cuota_bonificada)
                if commission.valor_cuota:
                    prices.append(commission.valor_cuota)
                # Common beca ratios: 50% of each known price
                for base in list(prices):
                    half = base / 2
                    if half not in prices:
                        prices.append(half)
            elif "pago único" in lower or "pago unico" in lower:
                cuota = commission.valor_cuota_bonificada or commission.valor_cuota
                cant = commission.cantidad_cuotas or 0
                if cuota and cant > 0:
                    prices.append(cuota * cant)

            if not prices:
                return []

            part_info.append((concept, prices))

        # Try all price combinations to find one that sums to the payment monto
        tolerance = Decimal("0.05")  # 5% tolerance on total
        generates_cobro = payment.movement is not None

        def _try_combinations(
            idx: int, remaining: Decimal, chosen: list[tuple[str, Decimal]]
        ) -> list[Allocation] | None:
            if idx == len(part_info):
                if remaining <= 0 or abs(remaining) / monto <= tolerance:
                    return [
                        Allocation(
                            payment=payment,
                            concept=concept,
                            amount=amount,
                            generates_venta=True,
                            generates_cobro=generates_cobro,
                        )
                        for concept, amount in chosen
                    ]
                return None

            concept, prices = part_info[idx]
            for price in prices:
                result = _try_combinations(idx + 1, remaining - price, chosen + [(concept, price)])
                if result is not None:
                    return result
            # Also try using the remaining amount directly for the last part
            if idx == len(part_info) - 1 and remaining > 0:
                result = _try_combinations(idx + 1, Decimal(0), chosen + [(concept, remaining)])
                if result is not None:
                    return result
            return None

        return _try_combinations(0, monto, []) or []

    @staticmethod
    def _sanitize_concepto(suggested: str, expected: str | None) -> str:
        """Ensure concepto is a valid single concept, not a combined one."""
        clean = suggested.strip()
        lowered = clean.casefold()
        # Valid patterns: "Inscripción", "Cuota N", "Pago Único", "Derecho Examen", etc.
        if "+" in clean or (" y " in lowered and "cuota" in lowered):
            # LLM invented a combined concept — use expected instead
            return expected.strip() if expected else clean
        return clean

    @staticmethod
    def _is_monto_plausible_for_concept(
        monto: Decimal,
        concept: str,
        commission: Commission,
        *,
        tolerance: Decimal | None = None,
    ) -> bool:
        """Check if the payment monto is plausible for the resolved concept.

        Prevents the LLM from labelling a $274.000 payment as "Inscripción"
        when the inscription price is $54.800.

        Default tolerance is 30%.  Callers can pass a higher tolerance (e.g.
        60% for LLM-validated allocations that may involve scholarships).
        """
        concept_lower = concept.strip().casefold()
        candidates: list[Decimal] = []

        if "inscripción" in concept_lower or "inscripcion" in concept_lower:
            if commission.valor_inscripcion_promocion:
                candidates.append(commission.valor_inscripcion_promocion)
            if commission.valor_inscripcion:
                candidates.append(commission.valor_inscripcion)
        elif "cuota" in concept_lower:
            if commission.valor_cuota_bonificada:
                candidates.append(commission.valor_cuota_bonificada)
            if commission.valor_cuota:
                candidates.append(commission.valor_cuota)
        elif "pago único" in concept_lower or "pago unico" in concept_lower:
            if commission.valor_pago_unico:
                candidates.append(commission.valor_pago_unico)
            cuota = commission.valor_cuota_bonificada or commission.valor_cuota
            cant = commission.cantidad_cuotas or 0
            if cuota and cant > 0:
                candidates.append(cuota * cant)

        if not candidates:
            return True  # Unknown concept type — allow

        max_tolerance = tolerance if tolerance is not None else Decimal("0.30")
        return any(
            abs(monto - target) / target <= max_tolerance
            for target in candidates
            if target > 0
        )

    _VALID_CONCEPT_RE = re.compile(
        r"^(Inscripción|Inscripcion|Inscripción Seminario|Inscripcion Seminario"
        r"|Cuota\s+\d+"
        r"|Pago Único|Pago Unico"
        r"|Derecho Examen"
        r"|Certificación|Certificacion)$",
        re.IGNORECASE,
    )

    @staticmethod
    def _is_valid_allocation_concept(concept: str | None) -> bool:
        if not concept:
            return False
        return bool(ConciliationPipeline._VALID_CONCEPT_RE.match(concept.strip()))

    def _plan_review(self, discrepancy: Discrepancy, reason: str) -> None:
        if discrepancy.actual_row is None:
            return
        row_number = discrepancy.actual_row.row_number
        self.patch_builder.add_flag(row_number=row_number, discrepancy_id=discrepancy.id, reason=reason)

    @staticmethod
    def _field_to_column(field: str | None) -> str | None:
        mapping = {
            "fecha_movimiento": "D",
            "concepto": "G",
            "monto": "H",
            "medio_pago": "I",
        }
        return mapping.get(field)
