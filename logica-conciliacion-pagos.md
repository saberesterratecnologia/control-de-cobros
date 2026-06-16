# Lógica de conciliación de pagos (fuente de verdad)

## 1. Resumen ejecutivo

El sistema de conciliación actual cruza **informes de pago** (`PAGO_MERCADO_PAGO`) contra **movimientos bancarios** (`MOVIMIENTO_BANCARIO`) y combina:

- flujo automático por coincidencia (persona + monto + fecha/mes/referencia),
- flujo asistido por planilla (API MAKE/Google Sheets),
- y operaciones manuales de backoffice en `inicio_cobros.aspx`.

La conciliación no es solo “encontrar un movimiento”: también gestiona estados operativos (`estado_conciliacion_auto`), reintentos (`fecha_proximo_intento_conciliacion`, `cantidad_intentos_conciliacion`) y casos manuales (deuda, inconsistencia, duplicados, etc.).

---

## 2. Tablas y modelo de datos

Referencias: `modelo/Modelo.dbml`.

### 2.1 `PAGO_MERCADO_PAGO` (núcleo del informe de pago)

Definición: `Modelo.dbml` líneas ~2192-2224.

Campos:

- `id_pago_mp` (Int, PK, identity): identificador del informe.
- `fecha` (DateTime NOT NULL): fecha declarada del pago.
- `monto` (Money NOT NULL): monto informado.
- `nro_operacion` (Char(50)): referencia/operación.
- `id_persona` (Int): alumno asociado.
- `id_medio_pago` (Int): FK a `MEDIO_PAGO`.
- `fecha_carga` (DateTime): timestamp de alta del informe.
- `controlado` (Bit): control manual humano.
- `nombre_archivo` (NText): comprobante adjunto.
- `comentario_cliente` (Char(255)): observaciones.
- `id_concepto_pago` (Int): FK a `PAGO_CONCEPTO`.
- `id_persona_logueada` (Int): quién cargó.
- `id_movimiento_bancario` (Int): vínculo al movimiento conciliado (`-1`/null = sin vínculo).
- `razon_social_originante` (Char(100)): originante declarado.
- `dni_cuit_originante` (Char(11)): CUIT/DNI originante.
- `archivo_factura` (Char(25)): archivo de factura emitida.
- `fecha_facturacion` (DateTime): fecha de factura.
- `id_organizacion` (Int): organización del pago.
- `ocr_json` (NVarChar(MAX)): resultado OCR del comprobante.
- `controlado_auto` (Bit NOT NULL): marca de control automático.
- `json_conciliacion` (NVarChar(MAX)): snapshot técnico del proceso de conciliación/planilla.
- `estado_conciliacion_auto` (NVarChar(50)): estado operacional de scheduler/auto.
- `fecha_ultimo_intento_conciliacion` (DateTime)
- `fecha_proximo_intento_conciliacion` (DateTime)
- `cantidad_intentos_conciliacion` (Int)
- `motivo_ultimo_intento_conciliacion` (NVarChar(100))
- `timestamp_comprobante_enviado` (DateTime): envío de notificación de pago procesado.

FKs:

- `id_medio_pago -> MEDIO_PAGO`
- `id_concepto_pago -> PAGO_CONCEPTO`
- `id_persona -> PERSONAS`

### 2.2 `MOVIMIENTO_BANCARIO`

Definición: `Modelo.dbml` líneas ~1241-1254.

- `id_movimiento` (Int, PK, identity)
- `id_cuenta_bancaria` (Int NOT NULL)
- `id_persona` (Int, nullable; en práctica `-1` para no identificado)
- `fecha` (Date NOT NULL)
- `referencia` (VarChar(50))
- `causal` (VarChar(100))
- `concepto` (VarChar(100))
- `importe` (Money NOT NULL)
- `conciliado` (Bit): si ya está vinculado a un informe.
- `json_identificacion` (NVarChar(2000)): trazabilidad del algoritmo de identificación de persona.

FK:

- `id_cuenta_bancaria -> CUENTA_BANCARIA`

### 2.3 `COMISIONES` (columnas relevantes a conciliación)

Definición: `Modelo.dbml` ~342+.

Relevantes:

- `id_comision`, `id_curso`, `id_organizacion`
- `nombre`
- `valor_inscripcion_promocion`, `valor_cuota_bonificada`, `cantidad_cuotas`
- `fecha_inicio`
- `borrado`

Se usan para mapear monto a conceptos (inscripción/cuotas) y generar filas de planilla.

### 2.4 `COMISIONES_PERSONAS` (relevante)

Definición: `Modelo.dbml` ~432+.

- `id_comision`, `id_persona`, `id_rol` (PK compuesta)
- `id_estado_academico`, `id_estado_administrativo`
- `eliminado`

Uso:

- Scope de conciliación por curso/comisión.
- Validación de estado admin (caso `manual_con_deuda` cuando `id_estado_administrativo=7` en API).

### 2.5 `PERSONAS` (relevante)

Definición: `Modelo.dbml` ~2029+.

- `id_persona`
- `nombres`, `apellidos`, `apellidos_nombres`
- `dni`
- `borrada`
- `email`

Uso: vincular pago/movimiento, render UI y detección de tokens de identificación.

### 2.6 `PAGO_CONCEPTO` (catálogo)

Definición: `Modelo.dbml` ~1470.

- `id_concepto_pago`
- `nombre`

Valores funcionales (documentados en código):

- `0 = NO DEFINIDO`
- `1 = INSCRIPCION A CURSO`
- `2 = CUOTA CURSO`
- `3 = DERECHO EXAMEN`
- `4 = RECARGO`
- `5 = INSCRIPCION A SEMINARIO`
- `6 = CERTIFICACION`

Fuente: comentario en `api_saberes.vb`, función `MapearConceptoXIdConceptoPago`.

### 2.7 `MEDIO_PAGO` (catálogo)

Definición: `Modelo.dbml` ~1086.

- `id_medio_pago`
- `nombre`

Observación de negocio en código:

- `id_medio_pago = 3` se interpreta como “pago profesor” y puede excluirse en `recuperar_medios(incluir_pago_profesor:=False)` (`pagos_mercadoPagoAccesoDatos.vb`).

### 2.8 `CUENTA_BANCARIA`

Definición: `Modelo.dbml` ~551.

Relevantes:

- `id_cuenta_bancaria`, `numero_cuenta`, `nombre_banco`
- `cuit_titular` (clave para excluir CUIT propio al identificar originantes)
- `id_organizacion`
- `ultimo_movimiento` (resumen textual de importación)

### 2.9 `ESTADOS_ACADEMICOS` / `ESTADOS_ADMINISTRATIVOS`

Definición: `Modelo.dbml` ~838 y ~846.

- `id_estado_*`, `nombre`, `descripcion`

Uso en conciliación: enriquecer filas de planilla y reglas de manualidad (deuda).

### 2.10 `ORGANIZACION`

Definición: `Modelo.dbml` ~2124.

Relevantes para este flujo:

- `id_organizacion`, `nombre`
- `id_perfil_correo`
- configuración usada para notificación/plantillas de comprobante procesado.

### 2.11 Otras tablas que participan

- `ORIGINANTE_PAGO`: catálogo de originantes alternativos por persona (CUIT/razón social), usado en identificación.
- `CURSOS`, `COMISIONES_PERSONAS`: delimitan scope del curso conciliable (id curso hardcodeado en API = 60).

---

## 3. Ciclo de vida del informe de pago

### 3.1 Creación

#### A) Desde `informar_pago/default.aspx.vb`

Alta con `accesoPa.insertar(pago)`:

- setea `controlado=False`, `id_movimiento_bancario=-1`, `estado_conciliacion_auto="pendiente"`, intentos en 0;
- guarda comprobante (`nombre_archivo`), persona, monto, referencia, originante;
- luego intenta conciliación inmediata: `accesoM.intentar_conciliar(pago.id_pago_mp)`.

#### B) Alta manual operador (detalles persona)

`detalles_personas.aspx.vb`, `lnk_generar_iPago_Click`:

- crea iPago con `controlado=True`, `id_movimiento_bancario=-1`, `estado_conciliacion_auto="pendiente"`;
- agrega `json_conciliacion` semilla: `{"id_movimiento_sugerido": X, "origen":"operador_detalles_personas"}`.

#### C) Alta directa administrativa

No se ve alta completa en `inicio_cobros.aspx.vb`; ahí predomina edición/control/manual de pagos existentes.

### 3.2 Estados operativos del informe

- `controlado`: validación manual humana.
- `controlado_auto`: validación automática de pipeline.
- `id_movimiento_bancario`: vínculo duro al movimiento.
- `estado_conciliacion_auto`: estado del scheduler/auto (pendiente/reintentar/manual/confirmado/timeout).

### 3.3 De “creado” a “procesado”

1. Se crea informe (`pendiente`, sin movimiento).
2. Puede autoconciliar por `intentar_conciliar`.
3. API planilla (`HandleConciliarPagoPlanilla`) calcula preview/manual/error.
4. Confirmación (`HandleConfirmarConciliacionPagoPlanilla`) persiste conciliación final o deja reintento/manual.
5. Flujo alterno “completar planilla” para pagos ya conciliados (`HandleCompletar...` + `HandleConfirmar...`).
6. Una vez confirmado y controlado, se habilita notificación de “pago procesado”.

---

## 4. Ciclo de vida del movimiento bancario

### 4.1 Importación/creación

Archivo: `modulos/administrativo/cuentas_bancarias/movimientos.aspx.vb`.

Durante import:

- filtra líneas válidas (importe > 0, referencia no vacía, etc.; en fragmento visto concepto `SETTLEMENT`);
- crea `MOVIMIENTO_BANCARIO` con `id_persona=-1`, `conciliado=False`;
- ejecuta `descubrir_persona_movimiento(m)`;
- guarda resultado en `id_persona` + `json_identificacion`;
- persiste con `insertar_validando`.

### 4.2 Lógica de identificación de `id_persona`

Fuente: `ConIgCba/auxiliar/ModGeneral.vb` (`descubrir_persona_movimiento`).

Orden:

1. parseo causal/CUIT-DNI;
2. match directo por DNI (`buscar_persona_x_originante`);
3. match por referencia contra informe (`recuperar_x_referencia`);
4. fallback tokens numéricos sobre `concepto+referencia` excluyendo CUIT titular de cuenta (`identificar_persona_x_tokens`).

### 4.3 `conciliado` flag

- `True`: movimiento ya tomado por un informe conciliado.
- `False`: candidato disponible.

La conciliación manual/auto lo pone en `True`; revertir lo vuelve `False`.

### 4.4 `json_identificacion`

Guarda diagnóstico del camino de identificación (motivo, tokens, candidatos, etc.) para auditoría y reintentos.

---

## 5. Algoritmo de conciliación automática

Archivo principal: `Negocio/movimiento_bancarioAccesoDatos.vb`.

### 5.1 `extraer_id_movimiento_sugerido(json)`

- Input: `json_conciliacion`.
- Lógica: regex `"id_movimiento_sugerido"\s*:\s*(\d+)`.
- Output: id (>0) o 0.

### 5.2 `intentar_conciliar(id_pago_mp)`

Paso a paso:

1. busca pago con `id_pago_mp` y `id_movimiento_bancario=-1`.
2. si en `json_conciliacion` hay `id_movimiento_sugerido`:
   - valida que exista movimiento, `conciliado=False`, `id_persona` igual al pago;
   - si cumple: vincula pago, `controlado=True`, marca movimiento `conciliado=True`, `SubmitChanges`, return id.
3. fallback por coincidencias:
   - mismo `id_persona`, `conciliado=False`, `importe == monto`, y (`fecha exacta` OR `mismo mes/año` OR `referencia == nro_operacion`).
4. si hay exactamente 1 movimiento:
   - setea `id_movimiento_bancario` en pago;
   - marca movimiento conciliado.
5. si 0 o múltiples: retorna `-1`.

### 5.3 `buscar_movimiento_conciliable(id_pago_mp)`

Misma regla de match que `intentar_conciliar`, pero solo devuelve `id_movimiento` o `-1`; no escribe nada.

### 5.4 `conciliar(id_pago_mp, id_movimiento)` (manual)

- setea en pago: `id_movimiento_bancario=id_movimiento`, `controlado=True`.
- setea en movimiento: `conciliado=True`.
- `SubmitChanges`.

### 5.5 `revertir_conciliacion_x_id_pago(id_pago_mp)`

- carga pago y movimiento asociado.
- pone movimiento `conciliado=False`.
- pone pago `id_movimiento_bancario=-1`, `controlado=False`.

### 5.6 `regularizar_pagos_generados_oficio()`

Migración legacy:

- detecta iPagos históricos “Generado autom...” ya conciliados sin `json_conciliacion`;
- les mete seed `id_movimiento_sugerido` + origen `migracion_oficio`;
- deconcilia movimiento, resetea estado/contadores a `pendiente` para que vuelvan a pasar por flujo planilla.

---

## 6. Flujo de conciliación por planilla (API)

Archivo: `ConIgCba/auxiliar/api_saberes.vb`.

### 6.1 `HandleConciliarPagoPlanilla`

Input:

- `id_pago_mp`, `anio`, `estado_cuenta` (planilla detectada).

Lógica principal:

1. valida pago y persona;
2. restringe scope a curso conciliable `ID_CURSO_CONCILIACION=60`;
3. estados de corte temprano:
   - `sin_comision`, `multiples_comisiones`, `manual_con_deuda`;
4. arma histórico real desde pagos previos (mapeo por `id_concepto_pago`, no por texto);
5. detecta duplicados (`manual_duplicado_probable`), inconsistencias (`manual_inconsistencia`), mapeo imposible (`manual_mapeo_pago`), monto no reconocido;
6. si mapea, construye movimientos objetivo (Venta/Cobro), compara con planilla y devuelve faltantes;
7. status preview: `preview`, `preview_sin_cambios`, `preview_solo_ventas`, `ya_procesado`.

Output:

- `conciliacion_response` con `status`, `message`, `id_movimiento_bancario`, `faltantes_planilla`, `historico_real`, `planilla_detectada`, `diferencias`, etc.

Persistencia:

- salvo `ya_procesado`, actualiza scheduler y `json_conciliacion`.

### 6.2 `HandleConfirmarConciliacionPagoPlanilla`

Input: `id_pago_mp`.

Lógica:

1. valida JSON previo.
2. si ya está conciliado: marca `confirmado` y actualiza scheduler.
3. si estado es manual/error (`manual_*`, `monto_no_reconocido`, `sin_comision`, etc.): responde error confirmable y deja estado manual.
4. si `preview_solo_ventas`: OK de ventas, conciliación pendiente para retry.
5. para `preview` / `preview_sin_cambios` intenta consolidar:
   - usa movimiento del preview si válido;
   - valida colisión con otro pago (`movimiento_bancario_ya_asignado` -> manual_inconsistencia);
   - si falla, `intentar_conciliar`.
6. si aún no hay movimiento:
   - `reintentar` a +1 día o `manual_timeout_sin_movimiento` si excede ventana.
7. si confirma:
   - `controlado=True`, `controlado_auto=True`, `status=confirmado`, actualiza ids cobro en `movimientos` tipo COBRO.

### 6.3 `HandleCompletarPlanillaPagoProcesado`

Escenario para pagos **ya conciliados y controlados** con `json_conciliacion` vacío.

- No escribe JSON (query only).
- Valida scope por curso/año, puede devolver `fuera_de_scope`.
- Genera faltantes de planilla y status `completo_generados_automaticamente`.

### 6.4 `HandleConfirmarCompletarPlanillaPagoProcesado`

Commit del flujo anterior:

- valida precondiciones;
- opcionalmente reevalúa scope;
- persiste `json_conciliacion` con `status=completo_generados_automaticamente` para evitar reproceso por polling MAKE.

---

## 7. Máquina de estados de conciliación

### 7.1 Estados de `estado_conciliacion_auto`

Usados por scheduler (`ActualizarSchedulerConciliacion*`):

- `pendiente`: listo para confirmar o recién creado.
- `reintentar`: no hay movimiento aún, próximo intento agendado.
- `confirmado`: conciliación efectiva.
- `manual`: requiere intervención humana.
- `manual_timeout_sin_movimiento`: timeout de ventana.

### 7.2 Estados técnicos en JSON/API

- `manual_con_deuda`
- `manual_inconsistencia`
- `manual_mapeo_pago`
- `manual_duplicado_probable`
- `monto_no_reconocido`
- `sin_comision`
- `multiples_comisiones`
- `preview`, `preview_sin_cambios`, `preview_solo_ventas`, `ya_procesado`, `fuera_de_scope`, `completo_generados_automaticamente`

Mapeo típico:

- `manual_*` y errores de negocio -> `estado_conciliacion_auto=manual` (excepto timeout explícito).
- preview sin movimiento -> `reintentar` o timeout.
- confirmado -> `confirmado`.

### 7.3 Retry

Campos:

- `fecha_ultimo_intento_conciliacion`
- `fecha_proximo_intento_conciliacion`
- `cantidad_intentos_conciliacion`
- `motivo_ultimo_intento_conciliacion`

Regla timeout (`DebePasarATimeoutConciliacion`):

- timeout si intentos >= 10 **o** fecha base + 10 días <= hoy.

Fecha base: `fecha_carga` (si existe), si no `fecha` del pago.

---

## 8. Operaciones manuales del operador (`inicio_cobros.aspx`)

### 8.1 Control manual

- `btn_controlado_Click` -> `actualizar_estado_control(True)`.
- `btn_noControlado_Click` -> `actualizar_estado_control(False)`.

### 8.2 Conciliar / revertir

- botón dinámico:
  - `CONCILIAR`: llama `accesoM.intentar_conciliar(id_pago_mp)`.
  - `REVERTIR CONCILIACIÓN`: desmarca movimiento y pone `id_movimiento_bancario=-1`.

### 8.3 Edición de pago

`lnk_aceptar_cobro_Click` permite editar:

- `monto`, `fecha`, `id_medio_pago`, `id_concepto_pago`,
- `comentario_cliente`, `nro_operacion`,
- `razon_social_originante`, `dni_cuit_originante`.

### 8.4 Comprobante / factura

- subida de comprobante de pago (`nombre_archivo`), admite imágenes/PDF.
- subida de factura (`archivo_factura`) y set de `fecha_facturacion`.

### 8.5 Notificación

- `btn_notificar_externo`: envía email “procesamos tu pago”.
- requiere perfil de correo + plantilla de organización.
- registra `timestamp_comprobante_enviado`.

---

## 9. Casos edge y modos de falla

### 9.1 Monto mismatch

- si no coincide contra inscripción/cuota(s) esperadas -> `monto_no_reconocido`.
- también hay fallos por mapeo de secuencia (`manual_mapeo_pago`).

### 9.2 Persona mismatch

- pago por tercero / originante ambiguo.
- identificación automática puede dejar `id_persona=-1` y requerir humano.

### 9.3 Múltiples movimientos posibles

- regla de conciliación exige 1 único match; si hay más de uno, no concilia (`-1`).

### 9.4 Duplicados

- detección explícita en API: `manual_duplicado_probable`.

### 9.5 Referencia faltante/incorrecta

- impacta identificación y match por referencia.
- el sistema depende entonces de monto+persona+fecha/mes.

### 9.6 Concepto incorrecto

- id_concepto no mapeable en flujo de planilla -> procesamiento sin movimientos/fuera de automatización completa.

### 9.7 Split / acumulados

- la API contempla casos de inscripción+cuota en un pago; pero escenarios fuera de ese patrón quedan en manual/monto no reconocido.

### 9.8 Timeout de conciliación

- si no aparece movimiento conciliable en ventana: `manual_timeout_sin_movimiento`.

---

## 10. Glosario

- **Informe de pago**: registro declarado por estudiante/operador (`PAGO_MERCADO_PAGO`).
- **Movimiento bancario**: extracto importado (`MOVIMIENTO_BANCARIO`).
- **Conciliación**: vincular informe con movimiento.
- **Planilla**: estado externo (Google Sheets) usado para reflejar Venta/Cobro esperados.
- **Preview**: simulación sin commit final de conciliación.
- **Scheduler retry**: metadata de reintentos automáticos en tabla de pagos.
- **Manual**: estado en el que se requiere resolución humana.

---

## Referencias de código (principal)

- `modelo/Modelo.dbml`
- `Negocio/movimiento_bancarioAccesoDatos.vb`
- `Negocio/pagos_mercadoPagoAccesoDatos.vb`
- `ConIgCba/auxiliar/api_saberes.vb`
- `ConIgCba/modulos/administrativo/cobros/inicio_cobros.aspx.vb`
- `ConIgCba/modulos/administrativo/cuentas_bancarias/movimientos.aspx.vb`
- `ConIgCba/auxiliar/ModGeneral.vb`
- `ConIgCba/informar_pago/default.aspx.vb`
- `ConIgCba/modulos/personas/detalles_personas.aspx.vb`
