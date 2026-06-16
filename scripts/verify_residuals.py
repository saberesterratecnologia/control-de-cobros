"""Query DB payments for residual cuota discrepancy DNIs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from main import load_config
import pyodbc

config = load_config("config/settings.yaml")
db = config["database"]

escaped_pwd = db['password'].replace("}", "}}")
conn_str = (
    f"DRIVER={{{db['driver']}}};"
    f"SERVER={db['server']};DATABASE={db['database']};"
    f"UID={db['username']};PWD={{{escaped_pwd}}};"
    f"TrustServerCertificate=yes"
)
sql = pyodbc.connect(conn_str)
cursor = sql.cursor()

residuals = [
    ("40295569", "VILLA MARIA", "Cuota 3 vs Cuota 1 in sheet (x2)"),
    ("47520420", "PARANA", "Cuota 2<>3 swap in sheet"),
    ("45836206", "MONTE MAIZ", "Cuota 3<>4 swap in sheet"),
    ("37761732", "VILLEGAS", "Cuota 1 vs Cuota 2 in sheet"),
]

for dni, comm_label, issue in residuals:
    print(f"\n{'='*70}")
    print(f"{comm_label} | DNI={dni}")
    print(f"Issue: {issue}")
    print(f"{'='*70}")

    cursor.execute("""
        SELECT p.id_pago_mp, p.fecha, p.monto, p.id_concepto_pago,
               p.id_movimiento_bancario, p.controlado,
               mb.fecha as mov_fecha, mb.importe as mov_importe
        FROM PAGO_MERCADO_PAGO p
        INNER JOIN PERSONAS per ON p.id_persona = per.id_persona
        LEFT JOIN MOVIMIENTO_BANCARIO mb ON p.id_movimiento_bancario = mb.id_movimiento
        WHERE per.dni = ?
        AND YEAR(p.fecha) = 2026
        ORDER BY p.fecha
    """, (dni,))

    payments = cursor.fetchall()
    concept_map = {1: "INSCR", 2: "CUOTA", 4: "RECARGO"}
    print(f"\n  DB Payments ({len(payments)}) chronological:")
    cuota_count = 0
    for i, p in enumerate(payments, 1):
        concept = concept_map.get(p[3], f"?({p[3]})")
        mov_info = f"mov_fecha={p[6]} mov_monto=${p[7]:,.0f}" if p[4] and p[4] > 0 else "no-movement"
        ctrl = "CTRL" if p[5] else ""
        
        if p[3] in (2, 4):
            cuota_count += 1
            expected_cuota = f"-> engine would assign: Cuota {cuota_count}"
        elif p[3] == 1:
            expected_cuota = "-> engine would assign: Inscripcion"
        else:
            expected_cuota = ""
        
        print(f"    #{i} id={p[0]} fecha={p[1]} monto=${p[2]:,.0f} db_concept={concept} {mov_info} {ctrl}")
        print(f"       {expected_cuota}")

sql.close()
