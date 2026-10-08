# 07 - Plantillas de WhatsApp (Meta / Twilio Content API)

Soporte para planeacion. Plantillas en espanol neutro (tuteo), listas para enviar a aprobacion de Meta via Twilio. Variables con `{{n}}` numeradas en orden, sin variables al inicio ni al final del body.

## Reglas generales que aplican a todas

- Categoria: UTILITY para transaccional (citas, recordatorios, reprogramacion, no-show, opt-out). MARKETING para resenas, prospeccion y promociones. Meta puede reclasificar: si rechaza una UTILITY por "promocional", reenviar como MARKETING.
- Nombre interno (`name`): snake_case, minusculas, solo `a-z0-9_`, unico por cuenta.
- Idioma: `es` (Meta usa `es` para espanol generico; ajustar si se usa `es_CO` en la cuenta).
- No incluir URLs cortas de acortadores ni emojis en el body de UTILITY. Usar boton URL (con sufijo variable al final) en lugar de pegar links.
- Datos personales (nombre, direccion, fecha de cita) son datos de salud indirectos en clinicas esteticas y odontologicas: enviar solo lo minimo necesario. No incluir motivo de consulta ni diagnostico. Ley 1581 de 2012: el paciente debe haber autorizado el tratamiento de sus datos y el canal WhatsApp.
- Consentimiento: los mensajes de MARKETING a leads (plantilla 7 y 6) solo se envian a numeros que aceptaron recibir contacto o que escribieron primero. Prospeccion en frio por WhatsApp viola politica de Meta y puede bloquear el numero emisor. Para leads del scraper, usar primero canal de email o llamada, o WhatsApp solo tras respuesta.
- Ventana de sesion de 24 h: dentro de 24 h desde el ultimo mensaje del usuario se puede enviar texto libre. Fuera de esa ventana, solo plantillas aprobadas.
- Respuestas: las opciones "1 / 2" se mapean a botones de respuesta rapida (quick reply) para que el bot capture la intencion sin NLP.

## Catalogo

| # | name | Categoria | Variables | Uso |
|---|------|-----------|-----------|-----|
| 1 | `cita_confirmacion` | UTILITY | 4 | Confirmacion tras agendar |
| 2 | `cita_recordatorio_24h` | UTILITY | 4 | 24 h antes |
| 3 | `cita_recordatorio_2h` | UTILITY | 4 | 2 h antes |
| 4 | `cita_reprogramada` | UTILITY | 4 | Cambio de horario |
| 5 | `cita_no_show` | UTILITY | 3 | Paciente no asistio |
| 6 | `resena_solicitud` | MARKETING | 2 + boton URL | Pedir resena tras cita |
| 7 | `lead_contacto_inicial` | MARKETING | 3 | Prospeccion (solo con consentimiento) |
| 8 | `optout_confirmacion` | UTILITY | 2 | Confirmar baja de mensajes |

### 1. `cita_confirmacion` (UTILITY)

- Body: `Hola {{1}}, tu cita en {{2}} quedo confirmada para el {{3}} a las {{4}}. Responde 1 para confirmar o 2 para reprogramar.`
- Variables: 1 nombre, 2 nombre del negocio, 3 fecha (ej. "martes 14 de octubre"), 4 hora (ej. "3:00 p. m.")
- Quick replies: `Confirmar` (id `confirmar`), `Reprogramar` (id `reprogramar`)
- Ejemplo: `Hola Ana, tu cita en Clinica Dental Sonrisa quedo confirmada para el martes 14 de octubre a las 3:00 p. m. Responde 1 para confirmar o 2 para reprogramar.`

### 2. `cita_recordatorio_24h` (UTILITY)

- Body: `Hola {{1}}, te recordamos tu cita de manana, {{2}}, a las {{3}} en {{4}}. Responde 1 para confirmar o 2 para reprogramar.`
- Variables: 1 nombre, 2 fecha, 3 hora, 4 negocio.
- Quick replies: `Confirmar`, `Reprogramar`.
- Si no hay respuesta en 6 h antes de la cita, el bot marca la cita como "sin confirmar" y avisa al staff en el panel.

### 3. `cita_recordatorio_2h` (UTILITY)

- Body: `Hola {{1}}, en 2 horas tienes tu cita en {{2}} a las {{3}}. Si no puedes asistir, responde 2 para reprogramar.`
- Variables: 1 nombre, 2 negocio, 3 hora.
- Quick replies: `Ya voy`, `Reprogramar`. Sin direccion en el body (evita variable al final y datos extra); la ubicacion se envia como mensaje de sesion aparte si la ventana sigue abierta, o se incluye en el 24 h.

### 4. `cita_reprogramada` (UTILITY)

- Body: `Hola {{1}}, tu cita en {{2}} fue reprogramada para el {{3}} a las {{4}}. Si este horario no te sirve, escribenos y te ayudamos a elegir otro.`
- Variables: 1 nombre, 2 negocio, 3 fecha nueva, 4 hora nueva.
- Quick replies: `Aceptar`, `Elegir otro horario`.

### 5. `cita_no_show` (UTILITY)

- Body: `Hola {{1}}, no alcanzamos a verte en tu cita de {{2}} el {{3}}. Si quieres agendar un nuevo horario, responde a este mensaje.`
- Variables: 1 nombre, 2 negocio, 3 fecha.
- Sin tono de reclamo ni cobro. Enviar maximo una vez por cita; no-show repetido (3 en 6 meses) lo decide el staff, no el bot.
- Quick replies: `Agendar`. (Sin variable al final.)

### 6. `resena_solicitud` (MARKETING)

- Body: `Hola {{1}}, gracias por visitar {{2}}. Tu opinion nos ayuda mucho. Dejanos tu resena con el boton de abajo.`
- Variables: 1 nombre, 2 negocio.
- Boton tipo URL: texto `Dejar resena`, URL base `https://g.page/r/{{1}}/review` donde `{{1}}` es el identificador de resena de Google del negocio (sufijo variable permitido en boton). Usar el ID real del negocio en Google Places.
- Enviar 24 a 48 h despues de la cita y solo si la cita fue atendida (no no-show).
- Cada negocio solo recibe una solicitud por paciente cada 90 dias.

### 7. `lead_contacto_inicial` (MARKETING)

- Body: `Hola {{1}}, somos del equipo de {{2}}. Vimos que {{3}} podria ahorrar tiempo con un recepcionista automatico que responde a tus clientes por WhatsApp las 24 horas. Te interesa una demo corta? Responde SI para recibir informacion o NO para no recibir mas mensajes.`
- Variables: 1 nombre del contacto (o "hola" si no hay nombre; no usar variable vacia), 2 nombre del agente/agencia, 3 tipo de negocio y necesidad concreta (ej. "tu clinica").
- Quick replies: `Si, quiero info`, `No, gracias` (la segunda dispara el flujo de opt-out, plantilla 8).
- Restricciones: solo a leads con consentimiento o que respondieron. Registrar fuente del dato y fecha de contacto (Ley 1581). Maximo un intento sin respuesta; no insistir.
- Nota: la variable 3 no debe prometer resultados medibles ("aumenta ventas 40%"). Meta rechaza claims no verificables.

### 8. `optout_confirmacion` (UTILITY)

- Body: `Listo, {{1}}. Dejamos de enviarte mensajes de {{2}}. Si cambias de opinion, escribenos en cualquier momento.`
- Variables: 1 nombre, 2 negocio o agencia.
- Se envia al confirmar `NO`, `STOP`, `BAJA` o boton de salida. Efectivo inmediato: marcar `opt_out=true` en el contacto y bloquear envios de MARKETING; los UTILITY de citas activas siguen permitidos solo si el paciente no pidio baja total (confirmar regla con legal).
- Palabras clave a reconocer (sin importar mayusculas): `STOP`, `BAJA`, `NO`, `CANCELAR`. Twilio/Meta ya manejan `STOP` a nivel de Messaging Service; el bot debe replicar la marca en su base.

## Twilio Content API - notas de implementacion

Base: `https://content.twilio.com/v1`. Autenticacion HTTP Basic con Account SID y Auth Token (o API Key). Credenciales en variables de entorno, nunca en el repo.

### Crear el contenido (una vez por plantilla)

```
POST https://content.twilio.com/v1/Content
Content-Type: application/json

{
  "friendly_name": "cita_confirmacion",
  "language": "es",
  "variables": {"1": "Ana", "2": "Clinica Dental Sonrisa", "3": "martes 14 de octubre", "4": "3:00 p. m."},
  "types": {
    "twilio/quick-reply": {
      "body": "Hola {{1}}, tu cita en {{2}} quedo confirmada para el {{3}} a las {{4}}. Responde 1 para confirmar o 2 para reprogramar.",
      "actions": [
        {"title": "Confirmar", "id": "confirmar"},
        {"title": "Reprogramar", "id": "reprogramar"}
      ]
    }
  }
}
```

- `variables` aqui son valores de ejemplo para la revision de Meta. Meta exige ejemplos representativos; sin ellos la plantilla se rechaza.
- Para plantillas sin botones usar `"twilio/text": {"body": "..."}`.
- Para boton URL con sufijo variable usar `"twilio/call-to-action"` con `actions` de tipo `URL` y `url` con `{{1}}`.

### Enviar aprobacion a WhatsApp

```
POST https://content.twilio.com/v1/Content/{ContentSid}/ApprovalRequests/whatsapp
{"name": "cita_confirmacion", "category": "UTILITY"}
```

Estados: `unsubmitted`, `received`, `pending`, `approved`, `rejected`. Consultar con `GET /v1/Content/{ContentSid}/ApprovalRequests`. Guardar el `ContentSid` (formato `HX...`) en la base, junto al nombre y la categoria. El bot solo puede usar plantillas en estado `approved`.

### Enviar un mensaje con plantilla

Via Messages API (no hace falta TwiML):

```
POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json
From=whatsapp:+<numero_twilio>
To=whatsapp:+<numero_paciente>
ContentSid=HXxxxxxxxx
ContentVariables={"1":"Ana","2":"Clinica Dental Sonrisa","3":"martes 14 de octubre","4":"3:00 p. m."}
```

- `ContentVariables` es JSON en string, claves `"1"`, `"2"`, etc.
- Enviar texto libre (`Body=`) solo dentro de la ventana de 24 h; el codigo debe verificar la ultima respuesta del usuario antes de elegir texto o plantilla.
- Para WhatsApp en Twilio, `StatusCallback` recibe `delivered`, `read`, `failed`. Guardarlos para metricas de citas confirmadas y para pausar reintentos si `failed` con error 63016 (fuera de ventana) o 63024 (plantilla no aprobada).

### Integracion en el proyecto

- Modulo sugerido: `app/integrations/whatsapp_templates.py` con un registro `TEMPLATE_REGISTRY` (`key -> {content_sid, category, variable_order}`) para no hardcodear SIDs.
- Las variables se pasan como lista ordenada y se convierten a `{"1": ..., "2": ...}` en un solo lugar.
- Pruebas: validar que el numero de variables de cada plantilla coincide con el registro (test unitario, sin llamar a Twilio).
- Cambiar una plantilla aprobada implica nueva plantilla (nuevo `name`), no edicion in situ.

## Pendientes para los planeadores

- Confirmar con legal si `optout_confirmacion` deja vivos los recordatorios de citas ya agendadas.
- Decidir si `resena_solicitud` queda en MARKETING (requiere opt-in mas estricto) o se cambia a un enlace enviado dentro de la ventana de sesion.
- Obtener el numero de WhatsApp Business verificado y el nombre de perfil antes de enviar cualquier plantilla de produccion.
