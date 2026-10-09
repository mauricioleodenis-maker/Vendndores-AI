# Revision haiku: reminders + worker (solo lectura)

Alcance: app/reminders/ (jobs.py, service.py, policies.py, scheduler.py), app/worker.py, app/web/templates/reminders/, tests/reminders/. Sin cambios de codigo.

## Hallazgos (ordenados por severidad)

### 1. ALTO - Envios duplicados de WhatsApp por reclamo de locks vencidos
- Archivos: app/reminders/service.py:381-396 (reclamo de `running` con `locked_at < now - STALE_LOCK`), service.py:189-195 (`process_job` solo verifica `status == "running"`, sin token de propiedad), app/reminders/jobs.py:26-29 (`_job_id` incluye el timestamp, asi que arq no deduplica), app/reminders/policies.py:79 (STALE_LOCK = 10 min), app/worker.py:113-114 (max_jobs=10, job_timeout=300).
- Escenario: si la cola de arq acumula mas de 10 min (o un envio tarda), el cron de 5 min reclama el job como pendiente, lo reencola con otro `_job_id` y lo vuelve a reclamar. Dos ejecuciones pasan `process_job` y envian dos mensajes al paciente.
- Escenario 2: service.py:274-277 envia por Twilio antes del commit (worker.py:37-44). Si el commit falla, hay rollback y reintento, y el mensaje ya salio.
- Fix: pasar un token de lock (locked_by + locked_at) como argumento de `reminders.send` y comparar en `process_job` antes de enviar; usar `_job_id=f"reminders:{jid}"` fijo; persistir un estado intermedio "enviando" con commit antes de llamar a Twilio y registrar el SID de Twilio; no reclamar jobs cuyo envio esta en vuelo.
- Tests: no hay prueba de doble ejecucion ni de reclamo durante un envio en curso (tests/reminders/test_reminders.py:445-457 solo cubre el reclamo basico).

### 2. ALTO (verificar base legal) - Recordatorios sin registro de consentimiento
- Archivos: app/reminders/service.py:104-118 (`_recordatorios_revoked` devuelve False si no hay ningun registro de Consent), app/reminders/scheduler.py:234-238 (`_contact_ok` solo revisa opted_out y erased_at).
- Efecto: un contacto sin ningun Consent con purpose "recordatorios" recibe mensajes. Solo se bloquea si hubo revocacion sin consentimiento activo.
- Fix: definir y documentar la base legal (necesidad contractual para una cita agendada, Ley 1581 de Colombia) en docs/plan/support/12 y en docs/revision/haiku-privacy.md; si se requiere consentimiento expreso, exigir un Consent activo y no solo ausencia de revocacion.

### 3. MEDIO - Fallos de encolado dejan jobs en `running` sin registro
- Archivo: app/reminders/jobs.py:23-29. El commit (linea 25) ocurre antes del `enqueue` por id. Si Redis falla a mitad del ciclo, la excepcion aborta el loop, los ids restantes quedan en `running` y no se reintentan hasta pasados 10 min. No hay log ni contador de fallos.
- Fix: encolar dentro de try por id; en fallo devolver el job a `pending` (o reclamar en el mismo commit), loguear `reminders.enqueue_failed` con conteo, y cubrir con prueba.

### 4. MEDIO - Texto del recordatorio puede mentir tras postergar por horario de silencio
- Archivos: app/reminders/service.py:251-257 (se posterga a 08:00 sin volver a renderizar), policies.py:99-111 (textos fijos: "en 2 horas", "tu cita de mañana").
- Ejemplos: cita 09:00 con recordatorio 2 h (run 07:00, postergado a 08:00) llega diciendo "en 2 horas" cuando faltan 1 h. Cita 20:30 del dia siguiente con recordatorio 24 h (run 20:30, postergado a 08:00) llega diciendo "de mañana" el mismo dia.
- Fix: calcular la frase relativa en el momento del envio (hoy/mañana, "en X horas" a partir de starts_at - now) o cancelar si la diferencia no coincide con la plantilla.

### 5. MEDIO - Configuracion faltante cae en silencio en el worker
- Archivo: app/worker.py:112 (`get_settings().redis_url or "redis://localhost:6379"`): un worker de produccion sin REDIS_URL se conecta a localhost sin error. app/worker.py:96 solo registra `missing_crons` en el log.
- Fix: en entorno prod lanzar excepcion si falta redis_url, y si faltan crons esperados (EXPECTED_CRON_NAMES) fallar el arranque en prod.

### 6. MEDIO - Cancelacion concurrente no frena un envio en curso
- Archivo: app/reminders/scheduler.py:273-283 marca `running` como cancelled, pero `process_job` (service.py:216-225) solo revisa el estado al inicio. Si la cita se cancela mientras el job envia, `_finish` sobrescribe el estado a `done` y el mensaje sale igual.
- Fix: revisar de nuevo el estado de la cita justo antes del envio, y en `_finish` no sobrescribir un `cancelled` escrito por otra transaccion (UPDATE condicional por status).

### 7. MEDIO (UI/UX, plan) - No hay interfaz de recordatorios
- Archivos: app/web/templates/reminders/ contiene solo .gitkeep (vacio); app/main.py no tiene rutas de recordatorios.
- Efecto: el tenant no puede ver, pausar ni cancelar recordatorios programados ni revisar los que fallaron (status `failed`). No se pueden evaluar copy en espanol, estados vacios ni comportamiento movil.
- Fix: confirmar en docs/plan si B10 exige UI. Si si: lista de jobs por cita con estado y motivo (`last_error`), accion de cancelar, estado vacio en espanol, y vista responsive; las consultas deben filtrar por tenant_id.

### 8. BAJO - Sustitucion secuencial de variables permite reescritura
- Archivo: app/reminders/policies.py:159-164. Los valores se reemplazan uno a uno; un nombre de contacto que contenga "{hora}" se reescribe al procesar la clave siguiente.
- Fix: sustitucion de una sola pasada con `re.sub` sobre un patron `\{\{?(\w+)\}?\}` que solo consulte el diccionario.

### 9. BAJO - Nombre enganoso de `in_quiet_hours`
- Archivo: app/reminders/policies.py:133-135. Devuelve True fuera de la ventana de envio (08:00-20:00); el nombre y el docstring de la constante no lo dejan claro.
- Fix: renombrar a `outside_send_window` o documentar la inversion; cubrir los limites 08:00 y 20:00 exactos en pruebas.

### 10. BAJO - Consultas por fila y busqueda LIKE sin indice de prefijo
- Archivos: service.py:191-231 y 233-251 (cerca de 8 consultas por job, incluidos dos conteos de Consent), app/reminders/service.py:319-326 (`LIKE prefix%` sobre dedupe_key; el indice unico btree no sirve para LIKE fuera de la collation C).
- Fix: cargar consentimiento, plantilla y bot config una vez por tenant en el lote; para el LIKE, usar rango (`dedupe_key >= prefix AND dedupe_key < prefix + '￿'`) o una columna `conversation_id` indexada.

### 11. BAJO - Brechas de prueba
- Faltan pruebas para: doble ejecucion con lock vencido (hallazgo 1), fallo de encolado (3), copy tras postergacion (4), cancelacion durante envio (6), y que un contacto sin consentimiento no reciba recordatorio (2).

## Verificado sin hallazgos
- Aislamiento por tenant: `process_job` valida tenant en job, contacto y cita (service.py:197-211); `_upsert_job` filtra por tenant (scheduler.py:210).
- Logs sin PII: solo job_id, kind, reason, tenant_id (service.py:276, jobs.py:43).
- Telefono descifrado solo al enviar (service.py:259) con AAD por tenant.
- No hay SSRF ni inyeccion de plantilla: `render_text` no usa str.format (policies.py:159).
