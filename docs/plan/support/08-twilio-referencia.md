# 08 - Referencia tecnica Twilio WhatsApp (Vendedores AI)

Documento de apoyo para planeacion y builders. Todo lo marcado con **[VERIFICAR]** debe confirmarse contra la documentacion oficial vigente de Twilio y Meta antes de implementar. Fuente de referencia: docs.twilio.com (WhatsApp, Messaging API, Content API, Request Validation, Voice TwiML).

## 1. Conceptos clave

- **Subaccount por tenant**: cada cliente (clinica, taller, restaurante) tiene su propio Subaccount de Twilio con su `AccountSid` y `AuthToken` propios. Aisla facturacion, numeros y limites.
- **Sender de WhatsApp**: numero registrado en WhatsApp Business (Meta) dentro del subaccount del tenant. Requiere verificacion del negocio en Meta Business Manager. **[VERIFICAR]** requisitos actuales de Meta y si el flujo de Embedded Signup aplica para ISV/Tech Provider.
- **Prefijo**: en Twilio todo remitente y destinatario WhatsApp va como `whatsapp:+E164`, ej. `whatsapp:+573001234567`.
- **Sandbox**: numero compartido de pruebas, no sirve en produccion.

## 2. Webhook entrante (mensaje del cliente)

Twilio hace POST `application/x-www-form-urlencoded` a la URL configurada en el sender (o en el Messaging Service). Ruta sugerida en el proyecto: `POST /webhooks/twilio/whatsapp`.

Parametros tipicos (**[VERIFICAR]** lista completa y nombres exactos):

| Parametro | Uso en el sistema |
|---|---|
| `MessageSid` / `SmsSid` | ID unico del mensaje. Usar como clave de idempotencia (SmsSid es alias legacy). |
| `AccountSid` | Identifica el subaccount (tenant) que recibio el mensaje. |
| `MessagingServiceSid` | Presente si se usa Messaging Service. |
| `From` | `whatsapp:+57...` del cliente final. |
| `To` | `whatsapp:+57...` del sender del negocio. Clave para resolver el tenant. |
| `Body` | Texto del mensaje. Puede venir vacio si solo hay media. |
| `NumMedia`, `MediaUrl0..N`, `MediaContentType0..N` | Adjuntos. Las URLs de media requieren autenticacion Basic con las credenciales del subaccount **[VERIFICAR]**. |
| `ProfileName` | Nombre de perfil de WhatsApp del remitente. Dato personal: tratar bajo Ley 1581. |
| `WaId` | Id WhatsApp del remitente (normalmente el numero sin `+`). |
| `ButtonText` / `ButtonPayload` | Respuesta de botones (quick reply). **[VERIFICAR]** nombres. |
| `Latitude` / `Longitude` | Si el cliente envia ubicacion. **[VERIFICAR]**. |
| `NumSegments`, `ApiVersion`, `SmsStatus` | Metadatos; no criticos. |

Reglas:
- Responder rapido con HTTP 200 (TwiML vacio o `<Response/>`). El procesamiento con Claude API va asincrono (cola/background task) para no pasar del timeout de Twilio (~15 s) **[VERIFICAR]**.
- Idempotencia: deduplicar por `MessageSid`; Twilio puede reintentar.
- Resolver tenant por `To` (numero del sender) y no solo por `AccountSid`, para validar que el numero pertenece al tenant.

## 3. Validacion de firma X-Twilio-Signature

Algoritmo:
1. Tomar la URL completa exacta que Twilio llamo (esquema, host, path y query string, sin alterar).
2. Si el request es POST con form-urlencoded: ordenar los parametros POST por nombre (orden ascendente, sensible a mayusculas **[VERIFICAR]**) y concatenar `nombre + valor` sin separadores, al final de la URL.
3. Calcular `HMAC-SHA1(key = AuthToken del subaccount, message = string del paso 2)`.
4. Codificar en Base64 y comparar con el header `X-Twilio-Signature` usando comparacion en tiempo constante.

Implementacion recomendada: `twilio.request_validator.RequestValidator(auth_token).validate(url, params, signature)`.

Trampas:
- Detras de proxy/load balancer, la URL que ve FastAPI puede ser `http://` o un host interno. Reconstruir la URL publica usando `X-Forwarded-Proto` / `X-Forwarded-Host` o una `PUBLIC_BASE_URL` fija en config. **[VERIFICAR]** comportamiento del proxy del hosting elegido.
- Multi-tenant: el AuthToken para validar es el del subaccount dueno del `To`, no un token global. Flujo: parsear form -> resolver tenant por `To` -> validar firma con su token -> procesar. Si no hay tenant, responder 403 sin procesar.
- Requests con body JSON (no form) usan el parametro `bodySHA256` en la URL **[VERIFICAR]**.
- Nunca loguear AuthToken ni la firma completa.

## 4. Envio de mensajes

Endpoint: `POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json`, auth Basic (`AccountSid` : `AuthToken` o API Key SID : API Key Secret; preferible API Key por tenant **[VERIFICAR]** buenas practicas).

Campos principales:
- `From`: `whatsapp:+<sender del tenant>` (o `MessagingServiceSid`).
- `To`: `whatsapp:+57...`.
- `Body`: texto libre. **Solo dentro de la ventana de 24 h.**
- `ContentSid` + `ContentVariables` (JSON string, ej. `{"1":"Maria"}`): envio de plantilla aprobada (Content API). Usar cuando la ventana esta cerrada o para outreach inicial.
- `MediaUrl`: adjuntos (imagen/pdf) **[VERIFICAR]** limites de tamano y tipos.
- `StatusCallback`: URL de status callbacks para este mensaje.

Respuesta: JSON con `sid` (SM...), `status` inicial (`queued`/`accepted`), `error_code` nulo.

### Plantillas (Content API)
- Crear: `POST https://content.twilio.com/v1/Content` con `types` (ej. `twilio/text`, `twilio/quick-reply`, `twilio/call-to-action`) **[VERIFICAR]** tipos vigentes.
- Variables: `{{1}}`, `{{2}}` en el body.
- `ContentSid` empieza por `HX`. Debe enviarse la plantilla **aprobada por Meta** (estado approved). Aprobacion puede tardar horas/dias **[VERIFICAR]**.
- Categorias: `utility`, `marketing`, `authentication`. El outreach comercial del agente cae en **marketing** (costo y politica mas estrictos) **[VERIFICAR]** precios por categoria en Colombia.

## 5. Ventana de sesion de 24 horas

- La ventana se abre cuando el **cliente** escribe al negocio y dura 24 h desde su ultimo mensaje entrante **[VERIFICAR]** (se renueva con cada mensaje del cliente).
- Dentro de la ventana: texto libre, media, botones.
- Fuera de la ventana: solo plantillas aprobadas. Enviar texto libre da error (ver seccion 7).
- Outreach de leads (lead finder) siempre es iniciado por el negocio: requiere plantilla aprobada y consentimiento/opt-out claro. Ley 1581: tratamiento de datos de contacto de terceros; documentar base legal.
- Regla operativa sugerida: el sistema guarda `last_inbound_at` por conversacion y decide texto libre vs plantilla antes de enviar (no depender solo del error de Twilio).

## 6. Status callbacks

Estados tipicos: `accepted` -> `queued` -> `sending` -> `sent` -> `delivered` -> `read`; o `failed` / `undelivered`. **[VERIFICAR]** lista exacta (`read` solo si el cliente tiene read receipts activos).

Campos del POST de callback: `MessageSid`, `MessageStatus`, `ErrorCode`, `ErrorMessage`, `To`, `From`, `AccountSid`.

Reglas:
- Validar firma igual que el webhook entrante (mismo tenant).
- Actualizar estado de forma monotona (no retroceder de `read` a `sent` por llegada desordenada).
- Persistir `ErrorCode` en el mensaje para reportes del panel admin.
- Se puede configurar callback global en el Messaging Service o por mensaje.

## 7. Codigos de error a manejar

Tabla orientativa. **[VERIFICAR] todos los codigos y su significado actual** en la pagina de errores de Twilio.

| Codigo | Significado aproximado | Accion sugerida |
|---|---|---|
| 63016 | Fuera de la ventana de 24 h (texto libre) | Reintentar con plantilla aprobada o marcar "requiere plantilla". |
| 63003 / 63007 | Canal o remitente WhatsApp no encontrado / mal configurado | Revisar sender del tenant; alerta admin. |
| 63018 / 20429 | Rate limit (demasiadas solicitudes / limite del sender) | Backoff exponencial y cola. |
| 63038 | Limite diario de mensajes del sender | Bloquear envios del tenant, alertar. |
| 63024 | Destinatario no es WhatsApp o no existe la cuenta **[VERIFICAR]** | Marcar contacto invalido. |
| 21211 | Numero destino invalido | Validar E.164 antes de enviar. |
| 21610 | Destinatario se dio de baja (STOP) | Marcar opt-out; no volver a enviar outreach. |
| 21614 | Numero no es movil | Descartar lead. |
| 21608 | Numero no verificado (cuentas trial) | Solo sandbox/trial. |
| 63049 | Politica de marketing / restriccion del usuario **[VERIFICAR]** | Suprimir outreach de marketing a ese contacto. |
| 5xxxx / 30xxx | Errores de red o del operador | Reintentar acotado; luego `failed`. |

Mapeo sugerido: `ErrorCode` -> enum interno `MessageFailureReason` con mensaje en espanol para el panel HTMX.

## 8. Subaccounts por tenant (multi-tenant)

- Crear subaccount por cliente via API (`POST /2010-04-01/Accounts.json` con `FriendlyName`) **[VERIFICAR]** permisos de la cuenta principal.
- Guardar por tenant: `AccountSid`, API Key SID y secreto cifrados (no el AuthToken en claro). Cifrado en reposo y clave fuera del repo.
- Aislamiento: todas las consultas a Twilio con credenciales del tenant; nunca usar la cuenta principal para enviar mensajes de clientes.
- Onboarding por tenant: alta de subaccount -> registro del sender WhatsApp (Meta) -> plantillas del tenant -> webhook URL configurada en el sender.
- Webhook URL: puede ser la misma ruta para todos los tenants; la resolucion se hace por `To`.
- Limites: confirmar costo por subaccount y cuotas **[VERIFICAR]**.

## 9. Sandbox para pruebas

Pasos (**[VERIFICAR]** numero y codigo vigentes en la consola):
1. Consola Twilio -> Messaging -> Try it out -> Send a WhatsApp message.
2. Desde WhatsApp en el telefono de prueba, enviar `join <codigo-de-dos-palabras>` al numero sandbox (referencia: +1 415 523 8886).
3. En la configuracion del sandbox, fijar "WHEN A MESSAGE COMES IN" a `https://<host-publico>/webhooks/twilio/whatsapp` (metodo POST).
4. Para desarrollo local: exponer el puerto con un tunel (ngrok u otro). La URL debe coincidir exacta con la usada para firmar.
5. El sandbox expira tras un periodo de inactividad (**[VERIFICAR]**, referencia 72 h): reenviar el `join`.
6. Probar: texto dentro de ventana, respuesta con firma invalida (debe dar 403), mensaje duplicado (idempotencia), callback de estado.
7. Las plantillas con aprobacion Meta no se prueban completas en sandbox; usar plantillas de sandbox o simular el callback.

## 10. Voice webhook basics (fase posterior)

- Llamada entrante: Twilio hace POST a la URL de voz con `CallSid`, `From`, `To`, `CallStatus`, `Direction`. Firma igual que WhatsApp (mismo algoritmo, token del subaccount).
- Respuesta en TwiML (XML): `<Say language="es-MX">`, `<Gather input="speech" language="es-CO" action="...">` para capturar voz (`SpeechResult`, `Confidence`), `<Redirect>` para volver al flujo.
- Para voz en tiempo real con IA: `<Connect><Stream url="wss://...">` abre websocket con audio (media streams). **[VERIFICAR]** latencia y formato de audio (mu-law 8 kHz) y costos.
- Status callback de llamada: `CallStatus` (`initiated`, `ringing`, `in-progress`, `completed`, `busy`, `no-answer`, `failed`).
- Ley 1581 y politica de grabacion: informar al llamante si se graba **[VERIFICAR]** requisitos.

## 11. Puntos a decidir en planeacion

- Modelo de credenciales por tenant (API Key vs AuthToken) y almacenamiento cifrado.
- Si el sender de cada tenant se registra por Embedded Signup o manualmente.
- Presupuesto de plantillas marketing para outreach y su aprobacion (tiempo de espera en el flujo de ventas).
- Ruta unica vs ruta por tenant para webhooks.
- Politica de reintentos y cola para respetar rate limits.
- Cumplimiento: almacenamiento de `ProfileName`, numeros y contenido de chats (datos de salud en clinicas estetica/odontologia -> Ley 1581, datos sensibles).

## 12. Checklist de verificacion pendiente

- [ ] Lista de parametros del webhook entrante y de callbacks (docs.twilio.com WhatsApp webhooks).
- [ ] Algoritmo exacto de firma y orden de parametros (Request Validation).
- [ ] Duracion de la ventana de 24 h y reglas de renovacion.
- [ ] Codigos de error y su significado actual (tabla seccion 7).
- [ ] Precios por categoria de plantilla en Colombia.
- [ ] Requisitos Meta para verificar negocio y aprobar sender por tenant.
- [ ] Instrucciones del sandbox (numero, join code, expiracion).
- [ ] Limites de media y de rate por sender.
- [ ] Tipos de Content API vigentes.
- [ ] Timeout de respuesta del webhook.
