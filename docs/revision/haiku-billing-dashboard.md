# Revision billing + dashboard (haiku)

Alcance: app/billing/, app/dashboard/, app/web/templates/billing/, app/web/templates/dashboard/, tests/billing/, tests/dashboard/.
Modo: solo lectura. No se editó código. Referencia: docs/plan/00-MAESTRO.md (roles §5 y B15), docs/revision-seguridad.md.

Hallazgos ordenados por severidad.

## 1. ALTO (verificar intencion): lectura y accion sobre conversaciones de todas las empresas sin filtro de tenant
- app/dashboard/router.py:461-551 y app/dashboard/service.py:161-209, 235-272
- Las rutas usan solo `Depends(current_user)`. Cualquier rol (incluido `operator`) lista conversaciones de todos los tenants, ve el telefono (enmascarado solo en plantilla, pero desencriptado en servidor, service.py:201), busca por texto (`q`) sobre todos los mensajes, y lee el transcript descifrado completo. `tomar`, `devolver` y `responder` tambien aceptan cualquier conversation_id sin confirmar que el operador pertenece a ese tenant.
- Atenuante: el MAESTRO §5 da al operador "tomar conversaciones", y la vista de transcript se audita (`conversation.view`). La lista y la busqueda NO se auditan.
- Fallo concreto: un operador consulta `/admin/conversaciones?q=<texto>` y obtiene coincidencias de la cartera completa sin registro de auditoria.
- Fix: confirmar con el producto si el operador es de agencia (cartera completa). Si es asi, auditar `conversation.list` y `conversation.search` con `diff={"q_len": n, "estado": ...}` (sin el texto), y documentarlo. Si no, pasar `tenant_id` del usuario en `list_conversations` y en `get_conversation`.
- Test faltante: ningun test cubre auditoria de la lista ni busqueda.

## 2. MEDIO-ALTO: doble envio de WhatsApp y envio antes de persistir en respuesta manual
- app/dashboard/service.py:317-362 (`send_manual_reply`), router.py:542-551
- Se valida `conv.status != "handoff"` y luego se envia por Twilio (linea 331) antes de hacer `session.add(msg)` y `flush` (linea 351-353). No hay bloqueo de fila (`SELECT ... FOR UPDATE`) ni clave de idempotencia.
- Fallo 1: doble clic en "Enviar" (el boton no se deshabilita) genera dos POST concurrentes; ambos pasan la validacion y el cliente recibe dos mensajes.
- Fallo 2: si Twilio acepta el mensaje y el `flush`/`commit` falla despues, el mensaje llega al cliente pero no queda en el transcript ni en la auditoria.
- Fix: (a) bloquear `conv` con `with_for_update()` al cargarlo; (b) persistir el `Message` en estado `queued` antes de llamar a Twilio y actualizarlo a `sent`/`failed` despues; (c) en el template, deshabilitar el boton al enviar (`onsubmit`) o usar un token de un solo uso en el formulario.
- Tests faltantes: envio concurrente y fallo de Twilio tras aceptar.

## 3. MEDIO: anular un cobro vencido no reactiva la suscripcion en mora
- app/billing/service.py:300-317 (`void_record`)
- `mark_paid` (linea 276-287) vuelve la suscripcion a `active` cuando ya no queda `vencido`. `void_record` no hace lo mismo: si el ultimo cobro vencido se anula, la suscripcion queda `past_due` para siempre (el job solo la pasa a `past_due`, no la regresa).
- Fix: extraer la logica de recuperacion a un helper `_recover_if_clear(session, subscription_id)` y llamarlo tambien desde `void_record` cuando el estado previo era `vencido`.
- Test faltante: `void` de un `vencido` en suscripcion `past_due` debe dejarla `active`.

## 4. MEDIO: vista de citas trunca en 200 filas y los conteos por empresa se calculan sobre la lista truncada
- app/dashboard/service.py:381-407 (`appointments_overview`, `.limit(200)` en linea 397; `per_tenant` en 404-406)
- El orden es por `starts_at`, asi que al superar 200 citas se pierden los dias finales sin aviso, y los contadores de "Citas por empresa" quedan subestimados. Tampoco filtra `Tenant.deleted_at`, a diferencia de `compute_kpis` (linea 127).
- Fix: calcular `per_tenant` con `SELECT tenant_id, count(*) ... GROUP BY` sin limite, paginar o mostrar aviso "mostrando 200 de N", y agregar `Tenant.deleted_at.is_(None)`.
- Template: app/web/templates/dashboard/citas_resumen.html:86 muestra los contadores; con dos empresas de igual nombre se fusionan (clave por nombre, service.py:406). Usar `tenant_id` como clave.

## 5. MEDIO: exportacion CSV y listado de facturacion truncados en silencio
- app/billing/router.py:516-536 (`limit=1000`), app/billing/router.py:444-450 (`list_records` sin paginacion, limite por defecto 500 en service.py:326)
- Un administrador que exporta o revisa mas de 1000 (o 500) cobros recibe un archivo o tabla incompleta sin advertencia. Es un fallo de integridad contable.
- Fix: exportar sin tope (streaming con `yield_per`) o al menos devolver `X-Total-Count` y un aviso visible; en la vista, paginar con `offset` y mostrar el total con `count()`.

## 6. MEDIO (rendimiento): busqueda de texto sin indice sobre todos los mensajes
- app/dashboard/service.py:183-192 (`Message.body_redacted.ilike('%q%')` dentro de `EXISTS`)
- El patron con comodin inicial no usa indices B-tree. Cada busqueda recorre `messages` completa de todos los tenants (no hay filtro de tenant ni de fecha), y el limite de 100 caracteres no evita el escaneo.
- Fix: indice GIN con `pg_trgm` sobre `body_redacted` (migracion), o limitar la busqueda por tenant y/o ventana de fechas. Medir con `EXPLAIN ANALYZE`.

## 7. MEDIO-BAJO: `create_record` usa la fecha UTC para vencimiento; el resto del modulo usa Bogota
- app/billing/service.py:136 (`due_date or utcnow().date()`)
- Entre 19:00 y 24:00 de Bogota la fecha UTC ya es el dia siguiente, por lo que el cobro vence un dia tarde y su `due_date` no coincide con la fecha local que ve el operador. El resto usa `to_bogota` (lineas 210, 76).
- Fix: `due_date or to_bogota(utcnow()).date()`. Test: crear a las 20:00 Bogota y verificar la fecha local.

## 8. MEDIO-BAJO: generacion mensual concurrente produce error 500 en lugar de respuesta controlada
- app/billing/router.py:500-513 y app/billing/jobs.py:17-27; app/db/models/plans.py:104-110 (indice unico parcial)
- La generacion manual y el cron de las 11:05 UTC pueden insertar la misma mensualidad. El indice unico `uq_billing_monthly` hace fallar el segundo `flush` con `IntegrityError`. La ruta no lo captura (500 al admin) y el job falla completo.
- Fix: insercion con `ON CONFLICT DO NOTHING` (`sqlalchemy.dialects.postgresql.insert(...).on_conflict_do_nothing(index_elements=[...], index_where=...)`) y contar filas afectadas con `RETURNING`.

## 9. BAJO: `mark_overdue` actualiza fila por fila en ORM
- app/billing/service.py:208-249 (bucle en 225-226)
- Carga todos los cobros pendientes vencidos como objetos ORM y los actualiza uno a uno. La revision previa (docs/revision-seguridad.md:25) pidio un UPDATE masivo; el cambio no se aplico.
- Fix: `update(BillingRecord).where(...).values(status="vencido")` y `returning(BillingRecord.subscription_id)` para obtener las suscripciones en mora en una sola consulta.

## 10. BAJO (rendimiento): `summary()` carga todas las suscripciones vivas para sumar en Python
- app/billing/service.py:345-355 (`mrr = sum(...)` sobre `select(Subscription, Plan)`)
- Se ejecuta en cada sondeo de KPIs cada 60 s (app/web/templates/dashboard/_kpis.html:66) para owner y admin.
- Fix: `select(func.coalesce(func.sum(func.coalesce(Subscription.custom_monthly_fee_cop, Plan.monthly_fee_cop)), 0), func.count())` con join; una sola consulta.

## 11. BAJO (verificar): resumen interno de la conversacion mostrado sin revisar PII
- app/web/templates/dashboard/conversacion.html:118 (`conv.summary`)
- `Conversation.summary` (app/db/models/conversations.py:66) se muestra tal cual. Si el resumen del bot conserva telefonos o correos, se expone en la vista sin `redact_pii`. Confirmar que el resumen se escribe ya redactado; si no, aplicar `redact_pii` al mostrar o al guardar.

## 12. BAJO: copy y UI
- app/web/templates/billing/facturacion.html:11 y 10: "Setups cobrados del mes" y "Vencido" usan jerga en ingles ("Setups"). Proponer "Instalaciones cobradas del mes" o "Implementacion cobrada del mes".
- facturacion.html:23: el enlace de exportacion concatena `estado` sin `urlencode` (hoy seguro porque `_status` valida contra `BILLING_STATUSES`, pero fragil).
- facturacion.html:43-55: los tres controles (select, input, boton) dentro de una celda de tabla en pantallas de 360 px forzan scroll horizontal; considerar una fila de detalle o un modal para registrar el pago.
- conversacion.html:132-143: el boton "Enviar" no se deshabilita tras el envio (ver hallazgo 2) y el contador de caracteres (maxlength 1000) no es visible.
- conversacion.html:127-129: `empty_state('Sin mensajes', '')` deja la descripcion vacia; agregar texto.
- conversaciones.html:168: el enlace usa el telefono enmascarado como unica etiqueta; agregar `aria-label` con el nombre del contacto o el id corto para lectores de pantalla.

## 13. Faltantes de pruebas
- tests/billing/test_billing.py: no cubre `void_record` sobre mora (hallazgo 3), anulacion de cobro en mora sin recuperacion, exportacion con mas de 1000 filas (hallazgo 5), ni `IntegrityError` concurrente en generacion (hallazgo 8).
- tests/dashboard/test_dashboard.py: no cubre auditoria de lista y busqueda (hallazgo 1), doble envio o fallo tras aceptar Twilio (hallazgo 2), citas truncadas o empresas eliminadas (hallazgo 4), ni la fecha de vencimiento en zona Bogota (hallazgo 7).

## Notas
- No se encontro SQL con interpolacion de usuario: la busqueda usa escape de `%` y `_` (service.py:184) y parametros enlazados.
- CSRF: `csrf_guard` se aplica a todos los routers `/admin` (app/main.py:135) y los formularios incluyen `csrf()`.
- Roles: `require_role("admin")` en billing (router.py:426) y `_can_see_billing` (owner/admin) en dashboard coinciden con el MAESTRO §5.
- Exportacion CSV: `neutralize_cell` se aplica a todas las celdas (service.py:404), incluidos campos de texto del usuario (reference, external_invoice_no).
