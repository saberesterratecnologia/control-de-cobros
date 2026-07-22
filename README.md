# Control de Cobros

Pipeline de conciliación entre pagos en SQL Server y la hoja Google Sheets de `COBROS`.

Este repo hace tres cosas:

1. detecta pagos conciliados en DB,
2. decide qué filas deberían existir en la hoja,
3. inserta o corrige filas sin tocar legacy de forma destructiva.

## Quick Path

1. Configurar `.env` con DB + Google credentials.
2. Ejecutar `python scripts/run_pipeline_only.py --dry-run` para ver impacto sin escribir.
3. Ejecutar `python scripts/run_pipeline_only.py --live` cuando el dry-run esté sano.

## Qué hace cada entrada

| Entrada | Uso | Nota |
|---|---|---|
| `main.py` | Pipeline + `estado_administrativo` | Encadena ambos flujos |
| `scripts/run_pipeline_only.py` | Solo conciliación de cobros | Recomendado para VPS/scheduler |
| `scripts/run_non60_live.py` | Live especializado para comisiones non-curso-60 | Caso operativo puntual |
| `scripts/health_check_cobros.py` | Auditoría estructural de la hoja | No escribe COBROS |

## Qué toca y qué no toca

| Recurso | Lo usa para | Escribe ahí |
|---|---|---|
| SQL Server | leer comisiones, alumnos, pagos y conciliaciones | No |
| `COBROS` | insertar/corregir filas operativas | Sí |
| `REVISIONES` | decisiones humanas reales | Sí |
| `LIMPIEZA_HOJA` | backlog de saneamiento no bloqueante | Sí |
| `data/context.db` | estado local del agente | Sí |

## Reglas operativas actuales

| Tema | Comportamiento |
|---|---|
| Política de escritura | Insert-only sobre legacy; no borra filas históricas automáticamente |
| Filas legacy sin `id_pago_mp` | Se preservan |
| `REVISIONES` | Solo casos que requieren decisión humana |
| `LIMPIEZA_HOJA` | Duplicados, secuencias, medios incorrectos, etc. |
| Agrupado de reviews | Casos de `concepto/fecha_movimiento` del mismo pago salen agrupados |

## Flujo actual

### 1. Lectura

- lee comisiones activas desde `COMISIONES`
- lee alumnos desde `COMISIONES_PERSONAS`
- lee pagos conciliados desde `PAGO_MERCADO_PAGO` + `MOVIMIENTO_BANCARIO`
- lee la hoja `COBROS`

### 2. Asignación

- usa precios de comisión (`inscripción`, `cuota`, `pago único`, `recargo`, `certificación`)
- asigna pagos a conceptos cuando puede en forma determinística
- usa LLM solo cuando la decisión no es obvia

### 3. Salida

- si falta fila: planifica insert
- si el valor está mal: planifica update
- si requiere decisión humana: manda a `REVISIONES`
- si es higiene no bloqueante: manda a `LIMPIEZA_HOJA`

## Colas humanas

### `REVISIONES`

Para decisiones humanas reales.

Ejemplos:

- `Revisar fecha/concepto | Pago 85916 - Cuota 8`
- `Requiere definición de concepto | Pago 80398`
- `Cuota excede el total`

Se completa escribiendo en la columna `resolucion`.

### `LIMPIEZA_HOJA`

Para saneamiento de la hoja.

Ejemplos:

- `Agregar inscripción`
- `Ordenar cuotas`
- `Corregir medio de cobro`
- `Quitar movimiento en venta`

Se completa con `estado`:

- `PENDIENTE`
- `HECHO`
- `IGNORAR`

## Comandos útiles

### Dry-run solo pipeline

```bash
python scripts/run_pipeline_only.py --dry-run
```

### Live solo pipeline

```bash
python scripts/run_pipeline_only.py --live
```

### Dry-run de una comisión

```bash
python scripts/run_pipeline_only.py --dry-run --commission "PERITO-S-CHIVILCOY-2026"
```

### Auditoría estructural

```bash
python scripts/health_check_cobros.py --commission "PERITO-S-CHIVILCOY-2026"
```

## Scheduler VPS

Para un VPS, el entrypoint recomendado es:

```bash
python scripts/run_pipeline_only.py --live
```

### Cron Linux a las 15:00

```cron
0 15 * * * /ruta/al/python /ruta/al/repo/scripts/run_pipeline_only.py --live >> /ruta/al/repo/data/pipeline_cron.log 2>&1
```

### Windows Task Scheduler

Programa:

```text
python
```

Argumentos:

```text
scripts/run_pipeline_only.py --live
```

Inicio en:

```text
C:\ruta\al\repo
```

## Estado actual del proyecto

- pipeline de cobros validado en dry-run sobre comisiones problemáticas
- separación entre `REVISIONES` y `LIMPIEZA_HOJA` ya implementada
- `REVISIONES` agrupada por pago para bajar ruido visual
- tests pasando
