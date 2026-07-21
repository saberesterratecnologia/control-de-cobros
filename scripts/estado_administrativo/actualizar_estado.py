"""Update id_estado_administrativo for students in curso 59/60 commissions.

Based on how many cuotas they have paid in the COBROS sheet and how old the
oldest missing cuota is on the calendar.

States:
  5 = Sin deuda (within grace window)
  6 = Con deuda 1 mes (from the 16th of the following month)
  7 = Con deuda 2 meses o mas (from the 1st of the second following month)

Only touches students whose current state is 5, 6, or 7 (or None).
States 1-4 (En funciones, De licencia, Renuncio, Desvinculado) are untouched.

Usage:
  # Dry-run all curso 59+60 commissions
  python scripts/estado_administrativo/actualizar_estado.py

  # Dry-run a specific commission
  python scripts/estado_administrativo/actualizar_estado.py --commission "PERITO-S-AMEGHINO-2026"

  # Dry-run a specific student
  python scripts/estado_administrativo/actualizar_estado.py --dni 20178034

  # Live (writes to DB)
  python scripts/estado_administrativo/actualizar_estado.py --live

  # Live + specific commission
  python scripts/estado_administrativo/actualizar_estado.py --live --commission "PERITO-D-FEBRERO-2026"
"""
from __future__ import annotations

from collections import Counter
import re
import sys
from datetime import date
from pathlib import Path

import click

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector
from src.connectors.sqlserver import SQLServerConnector

# States we are allowed to change
MUTABLE_STATES = {None, 5, 6, 7}

# Target courses
TARGET_COURSES = {59, 60}

CUOTA_RE = re.compile(r"Cuota\s+(\d+)", re.IGNORECASE)
GRACE_DAY = 15


def extract_max_cuota(concepto: str) -> int | None:
    """Extract cuota number from concepto like 'Cuota 3'."""
    m = CUOTA_RE.search(concepto)
    return int(m.group(1)) if m else None


def normalize_dni(value: str | int | None) -> str:
    """Normalize DNI/CUIT values to the 8-digit DNI form used in the sheet."""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11:
        return digits[2:10]
    return digits


def add_months(base: date, months: int) -> date:
    """Return the first day of the month *months* after *base*."""
    month_index = (base.month - 1) + months
    year = base.year + month_index // 12
    month = (month_index % 12) + 1
    return date(year, month, 1)


def unpaid_cuota_state(fecha_inicio: date, cuota_number: int, today: date) -> int:
    """Return the debt state caused by one unpaid cuota on *today*.

    Business rule:
    - Missing cuota is tolerated until the 15th of the following month.
    - From the 16th of that following month -> state 6.
    - From the 1st of the next month after that -> state 7.
    """
    cuota_month = add_months(date(fecha_inicio.year, fecha_inicio.month, 1), cuota_number - 1)
    month_gap = (today.year - cuota_month.year) * 12 + (today.month - cuota_month.month)

    if month_gap < 1:
        return 5
    if month_gap == 1:
        return 6 if today.day > GRACE_DAY else 5
    return 7


def expected_cuotas_paid(fecha_inicio: date, today: date) -> int:
    """Calculate how many cuotas a student should have paid by today.

    Missing cuota stays tolerated until the 15th of the following month.
    From the 16th onward it counts as expected/overdue for debt calculation.

    Example: start=Mar 2026
      today=Jul 15 -> expected=3 (through May)
      today=Jul 16 -> expected=4 (June becomes overdue)
      today=Aug 01 -> expected=4 (July still in grace window)

    If a student pays ahead, they stay in state 5 — no issue.
    """
    if today <= fecha_inicio:
        return 0

    months_diff = (today.year - fecha_inicio.year) * 12 + (today.month - fecha_inicio.month)

    if today.day <= GRACE_DAY:
        months_diff -= 1

    return max(0, months_diff)


def determine_new_state(
    cuotas_paid: set[int],
    fecha_inicio: date,
    today: date,
    total_cuotas: int,
) -> int:
    """Determine the new id_estado_administrativo.

    Returns 5 (sin deuda), 6 (1 mes), or 7 (2+ meses) based on the
    calendar age of the oldest unpaid cuota.
    """
    expected = expected_cuotas_paid(fecha_inicio, today)
    if total_cuotas > 0:
        expected = min(expected, total_cuotas)

    worst_state = 5
    for cuota_number in range(1, expected + 1):
        if cuota_number in cuotas_paid:
            continue
        worst_state = max(worst_state, unpaid_cuota_state(fecha_inicio, cuota_number, today))
        if worst_state == 7:
            return 7

    return worst_state


STATE_LABELS = {
    -1: "No aplica",
    1: "En funciones",
    2: "De licencia",
    3: "Renuncio",
    4: "Desvinculado",
    5: "Sin deuda",
    6: "Con deuda 1 mes",
    7: "Con deuda 2 meses o mas",
}


def transition_sort_key(item: tuple[tuple[str, str], int]) -> tuple[int, str, str]:
    """Sort transitions by severity first, then alphabetically."""
    (old_label, new_label), _count = item
    severity = {
        "Sin deuda": 0,
        "Con deuda 1 mes": 1,
        "Con deuda 2 meses o mas": 2,
    }
    return (
        severity.get(new_label, 99),
        old_label,
        new_label,
    )


def run_update(
    config: dict,
    live: bool = False,
    commission: str | None = None,
    cursos: tuple[int, ...] | None = None,
    dni: str | None = None,
) -> dict:
    """Run the estado administrativo update.

    Parameters
    ----------
    config:
        Already-loaded settings dict (from ``load_config``).
    live:
        If *True* write changes to DB; otherwise dry-run.
    commission:
        Optional substring filter for commission name.
    cursos:
        Course IDs to target.  Defaults to ``TARGET_COURSES`` when *None* or
        empty.
    dni:
        Optional DNI/CUIT filter for a single student.

    Returns
    -------
    dict
        Summary with keys: ``changes``, ``unchanged``, ``skipped``,
        ``errors``, ``pending_benefit``.
    """
    today = date.today()
    mode = "LIVE" if live else "DRY-RUN"
    click.echo(f"[{mode}] Estado Administrativo Update | Date: {today}")

    # Connect to sheet and DB
    sheets = SheetsConnector(config["sheets"])
    sheets.connect()
    all_rows = sheets.read_all_rows()
    click.echo(f"  Sheet rows loaded: {len(all_rows)}")

    sql = SQLServerConnector(config["database"])
    sql.connect()
    target_dni = normalize_dni(dni) if dni else None

    # Get target commissions
    target_cursos = cursos if cursos else TARGET_COURSES
    cursor = sql.connection.cursor()
    placeholders = ",".join("?" for _ in target_cursos)
    cursor.execute(f"""
        SELECT id_comision, id_curso, nombre, cantidad_cuotas, fecha_inicio
        FROM COMISIONES
        WHERE id_organizacion = 2
        AND borrado = 0
        AND YEAR(fecha_inicio) = 2026
        AND id_curso IN ({placeholders})
        AND id_estado_comision IN (2, 3, 4)
        AND analisis_pagos = 1
        ORDER BY nombre
    """, tuple(target_cursos))
    commissions = cursor.fetchall()

    if commission:
        commissions = [c for c in commissions if commission.upper() in c[2].upper()]

    click.echo(f"  Commissions: {len(commissions)}")

    # Build a lookup: (comision_name_stripped, dni_stripped) -> set of unique cuota numbers
    # from Venta rows in the sheet
    sheet_cuotas: dict[tuple[str, str], set[int]] = {}
    for row in all_rows:
        if not row.comision or not row.dni:
            continue
        if (row.tipo_movimiento or "").strip().casefold() != "venta":
            continue
        cuota_n = extract_max_cuota(row.concepto or "")
        if cuota_n is not None:
            key = (row.comision.strip(), row.dni.strip())
            sheet_cuotas.setdefault(key, set()).add(cuota_n)

    # Process each commission
    total_changes = 0
    total_unchanged = 0
    total_skipped = 0
    total_pending_benefit = 0
    results: list[dict] = []

    for comm in commissions:
        id_comision = comm[0]
        id_curso = comm[1]
        nombre = comm[2].strip()
        total_cuotas = comm[3] or 0
        fecha_inicio = comm[4]

        if fecha_inicio is None:
            click.echo(f"\n  [SKIP] {nombre}: no fecha_inicio")
            continue

        # Convert datetime to date if needed
        if hasattr(fecha_inicio, 'date'):
            fecha_inicio = fecha_inicio.date()

        expected = expected_cuotas_paid(fecha_inicio, today)

        # Get students for this commission
        cursor.execute("""
            SELECT cp.id_persona, p.dni, p.apellidos, p.nombres,
                   cp.id_estado_administrativo
            FROM COMISIONES_PERSONAS cp
            INNER JOIN PERSONAS p ON p.id_persona = cp.id_persona
            WHERE cp.id_comision = ?
            AND cp.eliminado = 0
            AND cp.analisis_pagos = 1
            AND p.borrada = 0
            AND cp.id_rol = 1
            AND cp.id_estado_academico IN (2, 4, 5, 6, 8, 9)
            ORDER BY p.apellidos, p.nombres
        """, (id_comision,))
        students = cursor.fetchall()
        if target_dni:
            students = [st for st in students if normalize_dni(st[1]) == target_dni]

        comm_changes = 0
        comm_skipped = 0
        comm_unchanged = 0

        for st in students:
            id_persona = st[0]
            dni = str(st[1]).strip()
            apellidos = st[2]
            nombres = st[3]
            current_state = st[4]

            # Normalize DNI (CUIT -> 8-digit extraction)
            dni_clean = normalize_dni(dni)

            # Skip immutable states (1-4)
            if current_state is not None and current_state not in MUTABLE_STATES:
                comm_skipped += 1
                total_skipped += 1
                continue

            # Look up cuotas paid in sheet
            key = (nombre, dni_clean)
            cuotas_in_sheet = sheet_cuotas.get(key, set())
            cuotas_paid_count = len(cuotas_in_sheet)

            new_state = determine_new_state(cuotas_in_sheet, fecha_inicio, today, total_cuotas)

            # Check if the MOST RECENT payment report is still pending
            # conciliation (no bank movement matched yet).  Only the last
            # report matters — older pending ones are stale.
            # Only cuota payments count (id_concepto_pago = 2) — a pending
            # inscription report doesn't justify waiving cuota debt.
            cursor.execute("""
                SELECT TOP 1 id_movimiento_bancario
                FROM PAGO_MERCADO_PAGO
                WHERE id_persona = ?
                AND YEAR(fecha) = 2026
                AND (id_organizacion = 2 OR id_organizacion IS NULL)
                AND id_concepto_pago = 2
                ORDER BY fecha DESC, id_pago_mp DESC
            """, (id_persona,))
            last_cuota_payment = cursor.fetchone()
            has_pending_report = (
                last_cuota_payment is not None
                and (last_cuota_payment[0] is None or last_cuota_payment[0] <= 0 or last_cuota_payment[0] == -1)
            )

            pending_benefit = False
            if has_pending_report:
                original_new_state = new_state
                if current_state == 5 and new_state in (6, 7):
                    # Al día + informe pendiente → benefit of the doubt,
                    # don't move to deuda
                    new_state = 5
                elif current_state in (6, 7) and new_state in (6, 7):
                    # En deuda + informe pendiente → the student is paying
                    # to recover access, lift them to sin deuda
                    new_state = 5
                if new_state != original_new_state:
                    pending_benefit = True

            if new_state == current_state:
                if pending_benefit:
                    total_pending_benefit += 1
                comm_unchanged += 1
                total_unchanged += 1
                continue

            old_label = STATE_LABELS.get(current_state, str(current_state))
            new_label = STATE_LABELS.get(new_state, str(new_state))

            results.append({
                "commission": nombre,
                "dni": dni_clean,
                "name": f"{apellidos}, {nombres}",
                "cuotas_paid": cuotas_paid_count,
                "expected": expected,
                "deficit": max(0, expected - cuotas_paid_count),
                "total_cuotas": total_cuotas,
                "old_state": current_state,
                "old_label": old_label,
                "new_state": new_state,
                "new_label": new_label,
                "id_persona": id_persona,
                "id_comision": id_comision,
                "pending_report": has_pending_report,
            })

            comm_changes += 1
            total_changes += 1

        click.echo(
            f"  {nombre}: {len(students)} students | "
            f"expected={expected}/{total_cuotas} cuotas | "
            f"changes={comm_changes} unchanged={comm_unchanged} skipped={comm_skipped}"
        )

    # Summary
    click.echo(f"\n{'='*70}")
    click.echo(f"TOTAL: {total_changes} changes | {total_unchanged} unchanged | {total_skipped} skipped (states 1-4)")
    if total_pending_benefit:
        click.echo(f"  Pending report benefit: {total_pending_benefit} students kept/lifted due to pending payment report")
    click.echo("NOTE: solo cuentan filas Venta con concepto Cuota N. Inscripcion no suma como cuota pagada.")
    click.echo("NOTE: una cuota entra en deuda el 16 del mes siguiente y escala el 1 del mes posterior.")

    if results:
        results_by_commission: dict[str, list[dict]] = {}
        for row in results:
            results_by_commission.setdefault(row["commission"], []).append(row)

        click.echo(f"\n{'='*70}")
        click.echo("CHANGES BY COMMISSION")

        for commission_name, commission_rows in results_by_commission.items():
            transition_counts = Counter(
                (row["old_label"], row["new_label"])
                for row in commission_rows
            )
            commission_rows.sort(
                key=lambda row: (
                    -row["deficit"],
                    row["new_label"],
                    row["cuotas_paid"],
                    row["name"],
                )
            )

            click.echo(f"\n{commission_name}")
            click.echo(f"  Total cambios: {len(commission_rows)}")
            click.echo("  Resumen por transicion:")
            for (old_label, new_label), count in sorted(transition_counts.items(), key=transition_sort_key):
                click.echo(f"    {count:>2} | {old_label} -> {new_label}")

            click.echo("  Detalle:")
            click.echo(f"    {'DNI':<10} {'Alumno':<28} {'Pagas':>5} {'Esp':>5} {'Def':>5}  Cambio")
            click.echo(f"    {'-' * 10} {'-' * 28} {'-' * 5} {'-' * 5} {'-' * 5}  {'-' * 35}")
            for row in commission_rows:
                pending_flag = " [P]" if row.get("pending_report") else ""
                click.echo(
                    f"    {row['dni']:<10} {row['name'][:28]:<28} "
                    f"{row['cuotas_paid']:>5} {row['expected']:>5} {row['deficit']:>5}  "
                    f"{row['old_label']} -> {row['new_label']}{pending_flag}"
                )

    total_errors = 0
    if live and results:
        click.echo(f"\n[LIVE] Applying {len(results)} updates to COMISIONES_PERSONAS...")
        applied = 0
        errors = 0
        for r in results:
            try:
                cursor.execute("""
                    UPDATE COMISIONES_PERSONAS
                    SET id_estado_administrativo = ?
                    WHERE id_persona = ? AND id_comision = ?
                """, (r["new_state"], r["id_persona"], r["id_comision"]))
                applied += 1
            except Exception as e:
                click.echo(f"  [ERROR] {r['commission']} DNI={r['dni']}: {e}")
                errors += 1

        sql.connection.commit()
        total_errors = errors
        click.echo(f"  Applied: {applied} | Errors: {errors}")
    elif not live and results:
        click.echo(f"\n[DRY-RUN] No changes applied. Use --live to apply.")

    sql.connection.close()

    return {
        "changes": total_changes,
        "unchanged": total_unchanged,
        "skipped": total_skipped,
        "errors": total_errors,
        "pending_benefit": total_pending_benefit,
    }


@click.command()
@click.option("--live", is_flag=True, default=False, help="Write changes to DB (default: dry-run)")
@click.option("--commission", default=None, help="Filter by commission name (substring match)")
@click.option("--curso", default=None, type=int, multiple=True, help="Filter by curso ID (can pass multiple, e.g. --curso 60 or --curso 59 --curso 60)")
@click.option("--dni", default=None, help="Filter by DNI/CUIT for a single student")
@click.option("--config", "config_path", default="config/settings.yaml")
def main(live: bool, commission: str | None, curso: tuple[int, ...], dni: str | None, config_path: str) -> None:
    """Update estado administrativo based on cuotas paid in COBROS sheet."""
    config = load_config(config_path)
    run_update(config=config, live=live, commission=commission, cursos=curso, dni=dni)


if __name__ == "__main__":
    main()
