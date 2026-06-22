"""Clear REVISIONES sheet and re-export all open reviews with the improved format.

Usage (from project root):
    .venv\Scripts\python.exe scripts\reexport_revisiones.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
from src.connectors.sheets import SheetsConnector
from src.context.context_manager import ContextManager
from src.reviews.review_manager import ReviewManager


def main() -> None:
    config = load_config("config/settings.yaml")
    sheets = SheetsConnector(config["sheets"])
    context = ContextManager(config["sqlite"]["db_path"])
    rm = ReviewManager(sheets, context, config)

    with context, sheets.connect():
        ws = rm._get_revisiones_sheet()  # noqa: SLF001
        if ws is None:
            print("[ERROR] No se pudo acceder a la pestana REVISIONES.")
            return

        all_values = ws.get_all_values()
        data_rows = len(all_values) - 1
        print(f"[INFO] Filas actuales en REVISIONES: {data_rows}")

        if data_rows > 0:
            ws.delete_rows(2, data_rows + 1)
            print(f"[OK] Borradas {data_rows} filas de datos")
        else:
            print("[INFO] La pestana ya estaba vacia")

        result = rm.export_to_sheet()
        print(f"[OK] Re-exportacion completada")
        print(f"  Exportadas: {result.get('exported', 0)}")
        print(f"  Saltadas: {result.get('skipped', 0)}")


if __name__ == "__main__":
    main()
