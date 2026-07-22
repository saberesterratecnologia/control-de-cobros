"""Run only the payment reconciliation pipeline.

Unlike ``main.py``, this script does NOT chain the administrative-status updater.
It is the right entrypoint for VPS scheduling when you only want COBROS,
REVISIONES and LIMPIEZA_HOJA maintenance.
"""

from __future__ import annotations

import logging as _logging
from pathlib import Path
from typing import Any
import sys

import click

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.observability.logger import StructuredLogger
from src.orchestrator.pipeline import ConciliationPipeline


@click.command()
@click.option("--dry-run/--live", default=True)
@click.option("--commission", default=None, help="Filter by commission name")
@click.option("--year", default=None, type=int, help="Year to process")
@click.option("--org", default=None, type=int, help="Organization ID to process")
@click.option("--skip-reviews", is_flag=True, help="Do not export REVISIONES/LIMPIEZA_HOJA after run")
@click.option("--force-reprocess", is_flag=True, help="Reprocess all payments including already-closed ones")
@click.option("--config", "config_path", default="config/settings.yaml")
@click.option("--verbose", is_flag=True)
def main(
    dry_run: bool,
    commission: str | None,
    year: int | None,
    org: int | None,
    skip_reviews: bool,
    force_reprocess: bool,
    config_path: str,
    verbose: bool,
) -> None:
    try:
        config = load_config(config_path)
        if year is not None:
            config.setdefault("agent", {})["year"] = year
        if org is not None:
            config.setdefault("agent", {})["id_organizacion"] = org
        config.setdefault("agent", {})["dry_run"] = dry_run

        logger = StructuredLogger("conciliation", log_file=config.get("logging", {}).get("file"))
        logger.log_run_start(config)

        pipeline_logger = _logging.getLogger("src.orchestrator.pipeline")
        if not pipeline_logger.handlers:
            console_handler = _logging.StreamHandler()
            console_handler.setFormatter(_logging.Formatter("%(message)s"))
            pipeline_logger.addHandler(console_handler)
            pipeline_logger.setLevel(_logging.INFO)

        pipeline = ConciliationPipeline(config)
        summary = pipeline.run(
            commission_filter=commission,
            dry_run=dry_run,
            skip_reviews=skip_reviews,
            force_reprocess=force_reprocess,
        )
        logger.log_run_summary(summary)

        click.echo("[OK] Pipeline-only run finished")
        click.echo(
            f"  Commissions: {summary.get('commissions_processed', 0)} | "
            f"Students: {summary.get('students_processed', 0)} | "
            f"Discrepancies: {summary.get('discrepancies_total', 0)}"
        )
        click.echo(
            f"  Auto: {summary.get('auto_fix', 0)} | "
            f"LLM: {summary.get('llm_decided', 0)} | "
            f"Review: {summary.get('pending_review', 0)} | "
            f"Cleanup: {summary.get('cleanup_tasks', 0)}"
        )
        if summary.get("sheet_anomalies", 0):
            click.echo(f"  Sheet anomalies: {summary.get('sheet_anomalies', 0)}")
        if summary.get("errors", 0):
            click.echo(f"  Errors: {summary['errors']}")
        if not dry_run and summary.get("run_id"):
            click.echo(f"  Run ID: {summary.get('run_id')}")
        if verbose:
            click.echo(f"  Reviews export: {summary.get('reviews_export', {})}")
            click.echo(f"  Cleanup export: {summary.get('cleanup_export', {})}")
            click.echo(f"  Patch summary: {summary.get('patch_summary', {})}")
            click.echo(f"  Writer: {summary.get('writer', {})}")
    except Exception as error:  # noqa: BLE001
        click.echo(f"[ERROR] Pipeline-only run failed: {error}", err=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
