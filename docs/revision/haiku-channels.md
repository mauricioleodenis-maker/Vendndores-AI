# Revisión de solo lectura: módulo `channels`

Alcance: `app/channels/` (router.py, service.py, sender.py, jobs.py), `tests/channels/`.
`app/web/templates/channels/` existe pero está vacío (no hay UI propia del módulo; el único
template que menciona canales es `app/web/templates/tenants/detail.html`).
Referencias: `docs/plan/00-MAESTRO.md` (§4 B6: firma, idempotencia por MessageSid, ruteo por número,
ventana 24 h, inbound -> privacy -> engine -> envío) y `docs/revision-seguridad.md`.

## Hallazgos (ordenados por severidad)

### Altos

1. **Derechos prometidos a la persona, no implementados (cumplimiento Ley 1581).**
   `jobs.py:36-42` (OPTOUT_REPLY y CONSENT_REPLY) promete "escribe *BORRAR MIS DATOS*" y
   "escribe *DERECHOS*". `is_erase_request` / `is_rights_request` (`app/privacy/keywords.py:50,54`)
   no tienen ningún llamador fuera de la re-exportación en `app/privacy/service.py`. Un paciente que
   escribe esas palabras recibe una respuesta del motor como cualquier otro mensaje.
   Fix: en `process()` (jobs.py, antes de `handle_inbound`, ~línea 232) despachar
   `is_erase_request` a `dsar.erase_contact` (con verificación de identidad o confirmación) y
   `is_rights_request` a una respuesta con el enlace de derechos. Añadir test.

2. **Se procesa al contacto antes de tener consentimiento.** `jobs.py:218-224` solo usa `consent`
   para decidir si enviar el aviso de privacidad y el ``SI``; el motor se ejecuta igualmente
   (`jobs.py:234`, `engine.handle_inbound`) para contactos sin consentimiento. El propio test
   `test_full_flow_first_message_sends_notice_then_reply` lo confirma: la primera respuesta del
   LLM sale en el mismo turno que el aviso. Los datos (nombre, teléfono, posibles datos de salud) se
   tratan y se envían al LLM antes del consentimiento.
   Fix: si `not consent`, enviar solo el aviso y retornar (mantener el mensaje sin procesar hasta el
   SI); o bloquear el motor hasta contar con consentimiento. Test: primer mensaje sin consentimiento
   no invoca `fake_llm`.

3. **Un fallo al encolar tras persistir pierde el mensaje.** `router.py:70-76`: se hace commit del
   mensaje y luego `enqueue(...)`. Si `enqueue` falla (p. ej. Redis caído), la petición responde 500,
   Twilio reintenta con el mismo MessageSid, `claim_event` ya está registrado y `router.py:68-69`
   devuelve TwiML sin encolar. El paciente nunca recibe respuesta y nadie lo registra.
   Fix: en el ramo duplicado, encolar si el mensaje sigue con `processed_at IS NULL` (o un barrido
   periódico de inbound sin procesar con más de N minutos). Test: fallo de `enqueue` seguido de
   reintento.

4. **Callbacks de estado se pierden si llegan antes de que el envío confirme el SID.**
   `service.py:270` reclama el evento (`claim_event`) antes de buscar el mensaje (`service.py:274-279`).
   El SID se escribe en `set_outbound_sent` dentro de la transacción del job (`jobs.py` vía
   `_deliver`), que se confirma al final. Si Twilio envía "delivered", "failed" o "21610" (STOP) antes
   de esa confirmación, `msg is None` devuelve `StatusResult(False)`, pero el `WebhookEvent` ya quedó
   confirmado: los reintentos se deduplican y el estado (y la baja por 21610, `service.py:414-415`)
   se pierde para siempre. Es un riesgo de cumplimiento (una baja no aplicada).
   Fix: buscar el mensaje antes de reclamar el evento; si no existe, no reclamar y responder 409/503
   para que Twilio reintente. Test: callback "21610" antes de que exista `provider_sid`.

5. **Reintentos no idempotentes al enviar (duplicados a pacientes).** `sender.py:196-215`:
   `_post_message` reintenta tras `httpx.HTTPError` (incluido ReadTimeout, cuando Twilio ya aceptó el
   mensaje) y ante 500/502/503/504 (`sender.py:38`). `POST /Messages` no tiene clave de idempotencia,
   así que cada reintento puede enviar otro WhatsApp. `test_live_retries_then_fails`
   (`tests/channels/test_sender.py:175`) fija ese comportamiento.
   Fix: reintentar solo errores de conexión (`ConnectError`, `ConnectTimeout`) y 429 con
   `Retry-After`; no reintentar ReadTimeout ni 5xx; registrar el intento para conciliar con el
   callback de estado.

### Medios

6. **Bajas (STOP) con alcance inconsistente: bloqueo silencioso que no se levanta.**
   `jobs.py:163` aplica la baja con `tenant_id` (alcance tenant), y `apply_optin` solo retira
   entradas de ese mismo alcance (`app/privacy/service.py:104-116`). Pero `sender.py:269` y `:288`
   llaman `is_suppressed(session, to_e164)` sin `tenant_id`, así que cualquier baja de cualquier
   clínica bloquea los envíos de todas. Un paciente que escribe SI a la clínica B no recibe respuesta
   de B y no hay forma de levantarlo desde B. El descarte solo queda en un `log.warning`.
   Fix: decidir la semántica y aplicarla de forma uniforme (pasar `tenant_id=tenant_id` en
   `sender.py:269` si la baja es por tenant), y registrar un evento visible cuando se suprime un envío.

7. **Fallo del motor silencioso.** `jobs.py:236-241`: si el motor lanza excepción, se marca el
   mensaje como procesado y se retorna 0. El paciente no recibe respuesta, no se abre handoff y no
   hay reintento. Es un fallo silencioso.
   Fix: enviar un mensaje de respaldo ("Un momento, te comunicamos con el equipo") y abrir un handoff
   (`open_handoff`), o reintentar con backoff acotado. Añadir métrica/alerta.

8. **Transacción abierta durante I/O de red.** `jobs.py:269` (`session_scope`) mantiene la transacción
   mientras `_deliver` hace HTTP con timeout de 10 s, hasta 3 intentos y backoff, por cada respuesta.
   Con varios envíos se bloquea una conexión del pool durante decenas de segundos.
   Fix: confirmar (o separar la sesión) antes de cada envío; o usar un outbox que lo envíe otro job.

9. **Sin límite de volumen por contacto ni por tenant.** Cada entrante dispara una llamada al LLM
   (`jobs.py:234`). `record_usage` solo suma (`app/plans/entitlements.py:180`); el plan no define un
   límite de mensajes (`_LIMIT_KEYS`, `entitlements.py:23-27`) y `assert_within_limit` solo se usa
   para conversaciones (`app/conversation/engine.py:259`). Un contacto puede generar costo ilimitado.
   Fix: limitar por contacto (p. ej. N mensajes por minuto, con la misma utilidad que usa el panel
   de pruebas del bot) y añadir la métrica `messages` a los planes.

10. **Consentimiento registrado por cualquier afirmación genérica.** `jobs.py:218`: `_is_yes` acepta
    "ok", "vale", "listo", "dale" como SI, y `record_consent` guarda siempre `"whatsapp:SI"` como
    evidencia (`jobs.py:220`), aunque el paciente haya escrito "ok" a otra pregunta.
    Fix: aceptar solo "SI" (y registrar el texto exacto recibido como evidencia) y exigir que el último
    mensaje del bot sea el aviso de privacidad.

11. **Primer mensaje solo multimedia sin aviso de privacidad.** `jobs.py:226-230` responde
    `MEDIA_REPLY` antes de la comprobación de consentimiento y sin enviar el aviso. Un contacto nuevo
    que envía un audio o imagen recibe una respuesta sin el aviso ni la información de tratamiento.
    Fix: enviar el aviso también en este ramo (o colocar el ramo después de la comprobación de aviso).

12. **Faltan pruebas de comportamientos de riesgo.** No hay tests de: fallo de `enqueue` tras commit
    (punto 3); callback antes de que exista el SID (punto 4); reintento tras ReadTimeout (punto 5);
    supresión entre tenants (punto 6); palabras BORRAR MIS DATOS / DERECHOS (punto 1); primer mensaje
    sin consentimiento sin llamar al LLM (punto 2); firma con más de 100 parámetros (punto 14).

### Bajos

13. **Mensajes sin `MessageSid` se descartan en silencio.** `service.py:222-223` devuelve
    `duplicate=True` y el webhook responde 200 sin registro. Fix: `log.warning` sin PII y 400 si Twilio
    no envía SID.

14. **Firma con más de 100 parámetros o claves repetidas.** `router.py:27,38-39` trunca en silencio a
    100 parámetros y sobrescribe claves repetidas (`params[key] = value`). Twilio firma todos los
    valores de una clave multivaluada, así que la firma no coincide y el webhook responde 403 sin
    causa visible. Fix: registrar un aviso al truncar y aceptar listas de valores en
    `validate_twilio_signature`.

15. **Condiciones de carrera al crear contacto o conversación.** `service.py:227` (y la creación de
    `Conversation` en `get_or_create_conversation`) hacen "consultar y luego insertar". Dos entrantes
    simultáneos del mismo número provocan `IntegrityError` en `uq_contacts_tenant_phone_hash` (500 y
    reintento de Twilio, que se resuelve sola) y pueden crear dos conversaciones abiertas. Fix: upsert
    (`ON CONFLICT DO NOTHING`) y índice parcial único de conversaciones abiertas.

16. **Código defensivo muerto y baja incompleta.** `jobs.py:209` usa `getattr(privacy, "apply_optin", None)`
    y, si faltara, la baja local se levanta sin borrar la supresión. `apply_optin` existe, así que basta
    llamarlo directamente. Al levantar la baja tampoco se limpia `opted_out_at` (`jobs.py:207-208`).

17. **Cliente HTTP por mensaje.** `sender.py:196` crea un `httpx.AsyncClient` por envío, sin reutilizar
    conexiones. Fix: un cliente compartido a nivel de módulo o de aplicación.

18. **Copy en español (texto visible en el panel admin y en TwiML).**
    - `sender.py:49` "Numero de destino invalido" -> "Número de destino inválido".
    - `sender.py:51` "El numero no es movil" -> "El número no es móvil".
    - `sender.py:52` "Numero no verificado (cuenta de prueba)" -> "Número no verificado (cuenta de prueba)".
    - `sender.py:53` "politica" -> "política".
    - `router.py:25` "Escribenos" -> "Escríbenos".
    - `router.py:44` "Firma invalida" -> "Firma inválida" (no es visible al usuario final, pero se muestra en
      logs y respuestas de depuración).
    - `jobs.py`: "Por ahora solo puedo leer mensajes de texto" está bien; revisar que el aviso mencione el
      canal y la finalidad en el mismo mensaje (hoy queda partido en dos).
    No hay UI del módulo en `app/web/templates/channels/` para revisar en móvil ni estados vacíos.

## Verificado y correcto (sin hallazgo)

- Firma Twilio: `hmac.compare_digest` (`sender.py:89`), orden de claves según la especificación,
  URL construida desde `PUBLIC_BASE_URL` y no desde el header `Host` (`service.py` `public_url`).
- Webhooks: ruteo por `To` con unicidad en `channel_accounts.phone_e164` (`tenants.py:109`) y filtro
  `Tenant.deleted_at`; token por tenant con respaldo de plataforma; firma validada antes de persistir.
- Idempotencia: `WebhookEvent` con `UniqueConstraint(provider, kind, provider_sid, status_value)`
  (`scheduling.py:68-70`) y `Message.provider_sid` único.
- Estados monótonos (`STATUS_RANK`) y ``status`` de Twilio sin retroceso.
- Aislamiento: las consultas de `process`, `apply_status` y `_window_open_for` filtran por `tenant_id`;
  el `phone_hash` es un índice ciego (`crypto.py:125`); `phone_enc` cifrado con AAD por tenant.
- Logs sin PII: los avisos registran IDs, códigos HTTP y nombres de error, nunca teléfono ni cuerpo.
- Erasure (DSAR): reemplaza `phone_hash` y borra cuerpos, por lo que un nuevo mensaje del mismo número
  crea un contacto nuevo (`dsar.py:162-166`); no hay fuga hacia el contacto borrado.
- Sin SSRF: el único destino saliente es `api.twilio.com` con URL fija.

## Propuesta de tests a añadir (tests/channels/)

- `test_enqueue_failure_is_retried_on_duplicate` (punto 3).
- `test_status_before_sid_is_not_claimed` y `test_21610_before_sid_applies_optout` (punto 4).
- `test_read_timeout_is_not_retried` (punto 5).
- `test_optout_in_tenant_a_does_not_block_tenant_b` o el comportamiento acordado (punto 6).
- `test_erase_and_rights_keywords_dispatch` (punto 1).
- `test_first_message_without_consent_does_not_call_llm` (punto 2).
- `test_media_first_message_sends_privacy_notice` (punto 11).
- `test_signature_with_over_100_params` (punto 14).
