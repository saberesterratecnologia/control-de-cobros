"""Run live conciliation for all non-curso-60 commissions (org=2, year=2026).

Designed to run in a separate process while the main session continues working.
Logs progress to data/live_non60_progress.log
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.orchestrator.pipeline import ConciliationPipeline

config = load_config("config/settings.yaml")
config.setdefault("agent", {})["year"] = 2026
config["agent"]["dry_run"] = False

pipeline = ConciliationPipeline(config)

# Get only valid non-curso-60 commission names.
# Excludes:
# - invalid commission states (only 2/3/4 are valid)
# - one-day seminars where fecha_inicio == fecha_finalizacion
sql = pipeline.sql
sql.connect()
cursor = sql.connection.cursor()
cursor.execute("""
    SELECT c.nombre
    FROM COMISIONES c
    WHERE c.id_organizacion = 2
    AND c.borrado = 0
    AND YEAR(c.fecha_inicio) = 2026
    AND c.id_curso != 60
    AND c.id_estado_comision IN (2, 3, 4)
    AND (
        c.fecha_finalizacion IS NULL
        OR CONVERT(date, c.fecha_inicio) <> CONVERT(date, c.fecha_finalizacion)
    )
    ORDER BY c.nombre
""")
commissions = [row[0].strip() for row in cursor.fetchall()]

progress_log = Path("data/live_non60_progress.log")

with open(progress_log, "w", encoding="utf-8") as log:
    log.write(f"Starting live run for {len(commissions)} non-curso-60 commissions\n")
    log.write(f"Commissions: {', '.join(commissions)}\n\n")
    log.flush()

    done = 0
    errors = []
    for name in commissions:
        done += 1
        log.write(f"[{done}/{len(commissions)}] {name} ... ")
        log.flush()
        try:
            start = time.time()
            summary = pipeline.run(
                commission_filter=name,
                dry_run=False,
                skip_reviews=True,
            )
            elapsed = time.time() - start
            disc = summary.get("discrepancies_total", 0)
            auto = summary.get("auto_fix", 0)
            llm = summary.get("llm_decided", 0)
            review = summary.get("pending_review", 0)
            students = summary.get("students_processed", 0)
            errs = summary.get("errors", 0)
            log.write(f"OK {elapsed:.0f}s | {students} students | {disc} disc ({auto} auto/{llm} llm/{review} review) | errors={errs}\n")
            if errs:
                errors.append(name)
        except Exception as e:
            log.write(f"FAILED: {e}\n")
            errors.append(name)
        log.flush()

    log.write(f"\n{'='*60}\n")
    log.write(f"DONE: {done}/{len(commissions)} commissions\n")
    if errors:
        log.write(f"ERRORS in: {', '.join(errors)}\n")
    else:
        log.write("All commissions completed successfully.\n")
    log.flush()

print(f"Finished {done}/{len(commissions)} commissions. Check data/live_non60_progress.log")
