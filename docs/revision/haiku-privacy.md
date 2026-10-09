# Revisión de módulo privacy (haiku)

Alcance: `app/privacy/`, `app/web/templates/privacy/policy.html`, `tests/privacy/test_privacy.py`.
Solo lectura. No se editó código. Ordenado por severidad.

## Alta

1. **Erasure incompleta (derecho de supresión, Ley 1581 art. 8).** `app/privacy/dsar.py:436-488`.
   `erase_contact` anonimiza `Contact`, `Message` (body_enc/body_redacted), `Conversation.summary` y `Appointment.notes_enc`, pero deja con datos personales:
   - `Handoff.summary` y `Handoff.reason` (`app/db/models/conversations.py:120`, creados en `app/conversation/handoff.py:66`): resumen de la conversación.
   - `Appointment.cancel_reason` (`app/db/models/booking.py:120`) y `Appointment.google_event_id` (`booking.py:116`): el evento de Google Calendar sigue existiendo con el nombre/teléfono del paciente; no se cancela ni elimina.
   - `Message.provider_sid`, `Message.llm_usage`, `Message.guardrail_flags` (no se usan como PII directa, pero conviene revisarlos).
   Fix: en `erase_contact` añadir `update(Handoff).where(conversation_id in conv_ids).values(summary="", reason="[eliminado]")`, `update(Appointment)...values(notes_enc=None, cancel_reason=None)`, y un job/llamada que cancele o borre el evento de Google (`google_event_id`). Añadir test que verifique cada columna.

2. **Exportación de acceso incompleta.** `app/privacy/dsar.py:394-424`.
   El paquete omite `Handoff` (resumen y motivo), `Conversation.summary` (resumen derivado de datos personales) y `Appointment.cancel_reason`. El titular no recibe todo lo que se almacena sobre él. Fix: incluir esos campos en `export_contact_data` y un test que compare contra la lista de columnas con PII.

3. **Authz de DSAR: cualquier owner/admin accede a cualquier tenant (riesgo de diseño, confirmar).** `app/privacy/router.py:52-73`.
   `require_role` (`app/core/deps.py:68-77`) solo mira `user.role`; `User` no tiene `tenant_id` (`app/db/models/users.py`). El `tenant_id` del path no se compara con ningún tenant del usuario. Según `docs/revision-seguridad.md:31` esto es intencional (la agencia ve todos los tenants). Pero el endpoint exporta y borra datos de salud/PII de clínicas. Fix recomendado: rol dedicado (`dpo`/`privacy_admin`), o al menos auditoría reforzada y confirmación de que el owner/admin es personal de la agencia. Documentar la decisión en `docs/revision-seguridad.md`.

## Media

4. **Slug truncado: coincidencia incorrecta.** `app/privacy/router.py:44`.
   `Tenant.slug == tenant_slug[:80]` acepta un slug de más de 80 caracteres si sus primeros 80 coinciden con un tenant real, y devuelve la política de ese tenant. Fix: rechazar `len(tenant_slug) > 80` con 404, o comparar con el valor completo (`Tenant.slug == tenant_slug`). Añadir test.

5. **Retención incompleta frente a la política publicada.** `app/privacy/retention.py:151-165`.
   Solo purga `Message.body_enc`/`body_redacted`. `Conversation.summary` (`conversations.py:66`), `Handoff.summary` (`conversations.py:120`) y `Appointment.notes_enc` no tienen purga y se conservan indefinidamente. La sección "Seguridad y retención" de `app/privacy/texts.py:130-134` dice que los mensajes se conservan un máximo de [N] meses; el resumen es dato derivado y no está cubierto. Fix: extender `purge_expired_messages` (o un job hermano) a resúmenes/notas, o documentar explícitamente la excepción.

6. **Placeholders legales publicados en la página pública.** `app/privacy/texts.py:72, 109-110, 133`; `app/web/templates/privacy/policy.html:176-181`.
   `/privacidad` renderiza `[NOMBRE AGENCIA]`, `[NIT]`, `[CORREO_DPO]`, `[TELEFONO]`, `[10]`, `[15]`, `[N]`, `[Confirmar base legal, art. 26 Ley 1581.]`. El aviso "Borrador" mitiga, pero el usuario final ve texto sin completar y el canal para ejercer derechos no existe. Fix: ocultar la página en producción hasta completar datos, o mostrar un bloque de contacto real desde configuración (`VAI_*`) y no desde texto fijo.

7. **N+1 y carga sin límite en la exportación.** `app/privacy/dsar.py:362-388`.
   Una consulta de mensajes por conversación, todos los mensajes sin paginar y en memoria. Para un contacto con mucho histórico, la petición puede ser lenta o agotar memoria. Fix: un solo `select(Message).join(Conversation).where(Conversation.contact_id == contact_id, Conversation.tenant_id == tenant_id).order_by(...)` agrupado en Python, o `selectinload`.

8. **Backfill de retención: un UPDATE por fila y carga total.** `app/privacy/retention.py:131-148`.
   Carga todos los `(id, created_at)` sin `purge_after` y ejecuta un UPDATE por cada uno. En el primer despliegue con muchos mensajes es lento y bloquea. Fix: una sola sentencia `update(Message).where(...).values(purge_after=cast(Message.created_at, Date) + timedelta(days=days))`, o `func.make_interval`; y procesar en lotes.

9. **Falta de tests que cubran los gaps anteriores.** `tests/privacy/test_privacy.py`.
   No hay tests de: erasure de `Handoff`/`cancel_reason`/evento de Google; export con handoffs; slug más largo de 80; N+1 de export; opt-in con campos residuales (ver 11); `record_consent` concurrente. `test_dsar_wrong_tenant` (línea 193) prueba solo el servicio, no el router ni el caso agencia.

## Baja

10. **Cron en UTC.** `app/privacy/jobs.py:27`. `hour={8}, minute={30}` es 03:30 en Bogotá. Usar `CRON_TZ` o documentar. Verificar con `app/worker.py` la zona horaria configurada.

11. **Opt-in deja campos residuales.** `app/privacy/service.py:193-199` y `app/channels/jobs.py:206-211`.
   `jobs.py` pone `contact.opt_out_source = None` antes de llamar a `apply_optin`, y ese `update` filtra por `Contact.opt_out_source == "keyword"`. Según el autoflush, la fila ya no coincide y `opt_out_keyword`/`opted_out_at` quedan con valores antiguos. Además `getattr(privacy, "apply_optin", None)` (jobs.py:209) es un fallback innecesario: la función existe. Fix: no tocar `opt_out_source` en ORM antes de la llamada, y llamar directamente a `apply_optin`. Verificar con un test de ciclo STOP -> START sobre DB real.

12. **Race en consentimientos.** `app/privacy/service.py:231-251`. La comprobación de "consentimiento vigente" y el INSERT no son atómicos y `Consent` no tiene índice único parcial sobre `(tenant_id, contact_id, purpose) WHERE revoked_at IS NULL` (`app/db/models/contacts.py:35-39`). Dos "SI" simultáneos pueden crear duplicados. Fix: índice único parcial en migración y `ON CONFLICT DO NOTHING`.

13. **Redacción: etiquetas erróneas.** `app/privacy/redaction.py:91-108`. `redact_pii("mi cedula 1.234.567.890")` devuelve `mi cedula [tel]` y `CC: 1098765432` devuelve `CC: [tel]`. No hay fuga (el dato se oculta), pero la etiqueta de analítica es incorrecta. Fix: aplicar `_ID_DOC` antes de `redact_text`, o rama específica.

14. **Erase no idempotente ni reversible.** `app/privacy/dsar.py:445-451`. Una segunda llamada sobrescribe `erased_at` y vuelve a generar `phone_hash`. Fix: si `contact.erased_at` ya existe, devolver sin cambios (o 409).

15. **Página de política sin estilos propios.** `app/web/templates/privacy/policy.html:170-183`. Las clases `.policy` y `.notice` no tienen CSS en `app/web/static` (búsqueda sin resultados). En móvil no hay tratamiento específico del aviso; verificar ancho de 16px de gutter y ausencia de scroll horizontal. Fix: añadir reglas para `.policy` (max-width, legibilidad) y `.notice` (contraste en modo oscuro).

16. **Copy en español.** `app/privacy/texts.py:14-19`. "Hola, soy el asistente virtual" se sustituye por `{negocio}` sin validar longitud ni caracteres; un nombre de negocio muy largo infla el SMS. Fix: truncar `negocio` a 60 caracteres antes de formatear.

## Lo que está bien

- Export y erase con `Cache-Control: no-store` y auditoría (`log_event`).
- Cifrado con AAD por tabla/columna/tenant (`app/privacy/dsar.py:309-316`).
- Supresión con alcance tenant/global y no se levanta por quejas (`service.py:177-202`).
- `redact_pii` enmascara teléfonos y correos correctamente (probado: `+57 300 123 4567` -> `[tel]`).
- Consultas de `Contact`/`Consent` filtran por `tenant_id`; índices de `phone_hash` existen.
