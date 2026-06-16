"""CLI entrypoint for the conciliation agent."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import click
import yaml
from dotenv import load_dotenv

from src.observability.logger import StructuredLogger
from src.orchestrator.pipeline import ConciliationPipeline


def load_config(path: str) -> dict[str, Any]:
    """Load YAML config and override secrets from environment variables."""
    # Load .env file into os.environ (does NOT overwrite existing env vars)
    load_dotenv()

    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    # --- Override database settings from env ---
    db = config.setdefault("database", {})
    if os.getenv("DB_SERVER"):
        db["server"] = os.environ["DB_SERVER"]
    if os.getenv("DB_DATABASE"):
        db["database"] = os.environ["DB_DATABASE"]
    if os.getenv("DB_USERNAME"):
        db["username"] = os.environ["DB_USERNAME"]
        db["trusted_connection"] = False  # si hay user/pass, no es Windows Auth
    if os.getenv("DB_PASSWORD"):
        db["password"] = os.environ["DB_PASSWORD"]

    # --- Override sheets settings from env ---
    sheets = config.setdefault("sheets", {})
    if os.getenv("GOOGLE_CREDENTIALS_PATH"):
        sheets["credentials_file"] = os.environ["GOOGLE_CREDENTIALS_PATH"]
    if os.getenv("SPREADSHEET_ID"):
        sheets["spreadsheet_id"] = os.environ["SPREADSHEET_ID"]

    # --- OPENAI_API_KEY: el SDK de OpenAI lo lee solo de os.environ ---
    # No hace falta inyectarlo en config, python-dotenv ya lo puso en os.environ

    return config


@click.command()
@click.option("--dry-run/--live", default=True)
@click.option("--commission", default=None, help="Filter by commission name")
@click.option("--year", default=None, type=int, help="Year to process (default: from config)")
@click.option("--org", default=None, type=int, help="Organization ID to process (default: from config, usually 2)")
@click.option("--skip-write-back", is_flag=True, help="Do not persist conciliation SQL updates")
@click.option("--export-reviews", is_flag=True, help="Only export pending reviews to REVISIONES")
@click.option("--sync-reviews", is_flag=True, help="Only sync resolved reviews from REVISIONES")
@click.option("--rollback", "rollback_run_id", default=None, help="Rollback a previous live run by run_id")
@click.option("--skip-reviews", is_flag=True, help="Do not export pending reviews to REVISIONES after run")
@click.option("--force-reprocess", is_flag=True, help="Reprocess all payments including already-closed ones")
@click.option("--config", "config_path", default="config/settings.yaml")
@click.option("--verbose", is_flag=True)
def main(
    dry_run: bool,
    commission: str | None,
    year: int | None,
    org: int | None,
    skip_write_back: bool,
    export_reviews: bool,
    sync_reviews: bool,
    rollback_run_id: str | None,
    skip_reviews: bool,
    force_reprocess: bool,
    config_path: str,
    verbose: bool,
) -> None:
    """Conciliation Agent - Payment reconciliation between SQL Server and Google Sheets."""
    try:
        config = load_config(config_path)
        if year is not None:
            config.setdefault("agent", {})["year"] = year
        if org is not None:
            config.setdefault("agent", {})["id_organizacion"] = org
        config.setdefault("agent", {})["dry_run"] = dry_run
        config.setdefault("agent", {})["skip_write_back"] = skip_write_back

        logger = StructuredLogger("conciliation", log_file=config.get("logging", {}).get("file"))
        logger.log_run_start(config)

        # Show pipeline progress on console
        import logging as _logging
        pipeline_logger = _logging.getLogger("src.orchestrator.pipeline")
        if not pipeline_logger.handlers:
            console_handler = _logging.StreamHandler()
            console_handler.setFormatter(_logging.Formatter("%(message)s"))
            pipeline_logger.addHandler(console_handler)
            pipeline_logger.setLevel(_logging.INFO)

        pipeline = ConciliationPipeline(config)

        selected_modes = sum(bool(v) for v in [export_reviews, sync_reviews, rollback_run_id is not None])
        if selected_modes > 1:
            click.echo("[ERROR] Use only one of: --export-reviews, --sync-reviews, --rollback", err=True)
            raise SystemExit(1)

        if export_reviews:
            result = pipeline.export_reviews()
            click.echo("[OK] Reviews exported")
            click.echo(f"  Exported: {result.get('exported', 0)} | Skipped: {result.get('skipped', 0)}")
            return

        if sync_reviews:
            result = pipeline.sync_reviews()
            click.echo("[OK] Reviews synced")
            click.echo(f"  Synced: {result.get('synced', 0)}")
            errors = result.get("errors", [])
            if errors:
                click.echo("  Errors:")
                for err in errors:
                    click.echo(f"   - {err}")
            return

        if rollback_run_id:
            result = pipeline.rollback(rollback_run_id)
            click.echo("[OK] Rollback finished")
            click.echo(f"  Run ID: {rollback_run_id}")
            click.echo(f"  Restored: {result.get('restored', 0)} | Failed: {result.get('failed', 0)}")
            errors = result.get("errors", [])
            if errors:
                click.echo("  Errors:")
                for err in errors:
                    click.echo(f"   - {err}")
            return

        summary = pipeline.run(
            commission_filter=commission,
            dry_run=dry_run,
            skip_reviews=skip_reviews,
            force_reprocess=force_reprocess,
        )
        logger.log_run_summary(summary)

        click.echo("[OK] Pipeline finished")
        click.echo(
            f"  Commissions: {summary.get('commissions_processed', 0)} | "
            f"Students: {summary.get('students_processed', 0)} | "
            f"Discrepancies: {summary.get('discrepancies_total', 0)}"
        )
        click.echo(
            f"  Auto: {summary.get('auto_fix', 0)} | "
            f"LLM: {summary.get('llm_decided', 0)} | "
            f"Review: {summary.get('pending_review', 0)}"
        )
        if summary.get("sheet_anomalies", 0):
            click.echo(f"  Sheet anomalies: {summary.get('sheet_anomalies', 0)}")
        if summary.get("errors", 0):
            click.echo(f"  Errors: {summary['errors']}")
        if not dry_run and summary.get("run_id"):
            click.echo(f"  Run ID: {summary.get('run_id')} (use --rollback <run_id> to undo)")
        if verbose:
            click.echo(f"  Run ID: {summary.get('run_id')}")
            click.echo(f"  Patch summary: {summary.get('patch_summary', {})}")
            click.echo(f"  Writer: {summary.get('writer', {})}")

        # --- Admin status update (chained after successful pipeline run) ---
        try:
            from scripts.estado_administrativo.actualizar_estado import run_update as update_admin_status

            click.echo("\n" + "=" * 70)
            click.echo("[ADMIN STATUS] Running estado administrativo update...")
            admin_summary = update_admin_status(
                config=config,
                live=not dry_run,
                commission=commission,
            )
            click.echo(
                f"[ADMIN STATUS] Done — Changes: {admin_summary.get('changes', 0)} | "
                f"Unchanged: {admin_summary.get('unchanged', 0)}"
            )
        except Exception as admin_err:  # noqa: BLE001
            click.echo(f"[ADMIN STATUS] Failed (non-fatal): {admin_err}", err=True)
    except Exception as error:  # noqa: BLE001
        click.echo(f"[ERROR] Pipeline failed: {error}", err=True)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
