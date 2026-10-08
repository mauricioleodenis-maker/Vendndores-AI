# 06 - Guion de ventas y objeciones (Vendedores AI)

Material de apoyo para ventas outbound y demo. Pensado para Cali, Colombia. Tono: cercano, directo, de tú (tuteo colombiano neutro, sin modismos forzados). Nunca prometer resultados garantizados. Precios en COP: usar los del plan vigente (`[PRECIO_PLAN]`), no inventar cifras.

Reglas de cumplimiento a respetar en todo el guion:
- Ley 1581 de 2012 (habeas data): no enviar mensajes masivos a personas sin base; ofrecer opción clara de salida ("responde NO y no te escribo más"). Datos de salud de pacientes: el bot no los almacena más de lo necesario y el cliente final debe autorizar el tratamiento.
- Meta / WhatsApp Business: mensajes de primer contacto con plantilla o con respuesta del usuario; sin spam; sin prometer lo que la API no hace.
- El bot solo habla del negocio del cliente (citas, precios, horarios, ubicación). No da diagnósticos médicos.

---

## 1. Primer mensaje de WhatsApp (con "prueba secreta")

Se envía al dueño/administrador del negocio, tras haber identificado el negocio con el lead finder. Una sola vez; si no responde en 3 días, pasa a la secuencia de seguimiento (sección 5).

**Versión A (dentista / clínica estética):**
> Hola [NOMBRE], soy [TU_NOMBRE] de Vendedores AI en Cali. Vi que [NOMBRE_CLINICA] atiende pacientes por WhatsApp. Tenemos una *prueba secreta*: una recepcionista con IA que agenda citas 24/7 y responde precios sin que tu equipo tenga que dejar lo que está haciendo. Te la muestro en 2 minutos y si no te sirve, no pasa nada. ¿Te la envío?

**Versión B (taller mecánico):**
> Hola [NOMBRE], soy [TU_NOMBRE] de Vendedores AI. Vi que [NOMBRE_TALLER] recibe muchos mensajes de cotizaciones. Estamos probando con algunos talleres de Cali una *prueba secreta*: un asistente que responde cotizaciones y agenda revisiones solo, mientras tú trabajas. ¿Quieres verla?

**Versión C (restaurante):**
> Hola [NOMBRE], soy [TU_NOMBRE] de Vendedores AI. Un asistente de WhatsApp que toma reservas y responde el menú y horarios mientras atiendes mesas. Tenemos una *prueba secreta* para restaurantes de Cali. ¿Te interesa que te la muestre?

Notas de uso:
- No usar "prueba secreta" si el lead ya dijo que no. No enviar más de un mensaje sin respuesta antes del día 3.
- Si responden "¿quién eres?" o "¿de dónde tienes mi número?": responder con la fuente pública (Google Maps/redes del negocio) y ofrecer salir de la lista.

---

## 2. Guion de llamada (2 a 4 minutos)

**Apertura (15 s)**
> Hola, ¿hablo con [NOMBRE]? Soy [TU_NOMBRE] de Vendedores AI, te escribí por WhatsApp sobre la prueba secreta de recepcionista con IA. ¿Tienes 2 minutos o prefieres que te escriba?

Si dice que no: "Perfecto, ¿te escribo mañana a esta hora?" y registrar en CRM.

**Diagnóstico (60 s)** - hacer 2 preguntas, escuchar:
1. "¿Cuántos mensajes de WhatsApp llegan al día y cuántos se quedan sin respuesta?"
2. "Cuando alguien escribe fuera de horario o mientras atienden, ¿qué pasa con esa cita?"

**Propuesta (45 s)**
> Lo que hacemos es poner un asistente en su WhatsApp que responde con la información de su negocio, agenda en Google Calendar y le pasa el caso a una persona cuando hace falta. Lo configuramos nosotros en unos días a partir de los datos básicos de su negocio.

**Demo (ver sección 3)** - ofrecer enviar un enlace o agendar 15 minutos.

**Cierre (30 s)**
> ¿Lo vemos juntos esta semana? Le muestro el bot con los precios y horarios de su clínica. ¿Martes o jueves en la tarde?

Registrar siempre: fecha, objeción principal, siguiente paso con fecha.

---

## 3. Guion de demo (10 a 15 minutos)

Objetivo: que vea SU negocio respondiendo. Antes de la demo, usar la fábrica de bots con los datos del prospecto (nombre, servicios, precios, horarios, dirección).

1. **Contexto (1 min).** "Te muestro el bot ya configurado con la información de [NEGOCIO]. Escríbelo tú mismo desde otro celular."
2. **Consulta de precio (2 min).** Pedir un servicio típico ("¿cuánto vale una limpieza?"). Mostrar que responde con el precio configurado.
3. **Agendamiento (3 min).** Pedir cita para mañana. Mostrar que consulta disponibilidad en Google Calendar y confirma.
4. **Fuera de tema (2 min).** Preguntar algo ajeno ("¿me recomiendas un restaurante?"). El bot redirige al negocio. Explicar: "Está diseñado para no salirse del tema de su negocio, y eso también cumple las políticas de WhatsApp."
5. **Escalamiento (2 min).** Pedir hablar con una persona o un caso urgente. Mostrar la notificación al dueño/equipo.
6. **Panel (2 min).** Mostrar conversaciones, citas y leads en el panel administrativo (Jinja2/HTMX).
7. **Cierre (2 min).** Preguntar: "¿Qué tendría que pasar para que lo activemos en esta semana?" Luego presentar el plan (`[PRECIO_PLAN]` COP) y la oferta de arranque.

Errores a evitar en la demo:
- No mostrar datos reales de pacientes. Usar datos ficticios o del propio prospecto con permiso.
- No prometer voz todavía si no está en el roadmap (la voz es fase posterior).
- No dejar que el prospecto pruebe con preguntas médicas: el bot no diagnostica.

---

## 4. Objeciones y respuestas (12)

Formato: objeción textual del prospecto, respuesta corta para decir, y el dato que la sostiene. No inventar cifras: donde diga `[DATO]`, completar con dato real de piloto.

**1. "Es muy caro."**
> Entiendo. ¿Cuánto les deja hoy una cita que se pierde por no contestar a tiempo? Si el bot recupera 2 o 3 citas al mes, el plan se paga solo. Te puedo mostrar el cálculo con tus números.
Dato: `[DATO_PILOTO: citas recuperadas promedio]`.

**2. "La IA responde mal / se inventa cosas."**
> El bot solo responde con la información que tú le das: precios, horarios, servicios. Si no sabe, dice que lo consulta con el equipo y te avisa. Lo puedes probar tú mismo antes de activarlo.
Dato: el bot se configura con base de conocimiento cerrada; se revisa en demo.

**3. "¿Y mis datos y los de mis pacientes?"**
> Los datos quedan en tu cuenta, no los usamos para otra cosa, y solo guardamos lo necesario para agendar. Para datos de salud, el paciente autoriza el tratamiento según la Ley 1581 y podemos firmar un acuerdo de tratamiento de datos contigo. Te envío el documento.
Nota interna: no afirmar cumplimiento total sin revisión legal; decir "diseñado para Ley 1581" y ofrecer el anexo.

**4. "Ya tengo secretaria / recepcionista."**
> Perfecto, el bot no reemplaza a tu secretaria: se encarga de los mensajes de fuera de horario y de las consultas repetidas (precios, horarios, dirección), para que ella se dedique a los pacientes que están en el consultorio. Muchas clínicas lo usan como respaldo.

**5. "No es para mi negocio, mis clientes quieren hablar con una persona."**
> Claro, por eso el bot escala a una persona cuando el cliente lo pide o el caso es urgente. Tú decides qué maneja el bot y qué pasa a tu equipo.

**6. "Ya uso ManyChat / otro chatbot."**
> Qué bueno que ya estás automatizando. ¿Tu chatbot agenda en tu calendario y responde con IA, o funciona con botones? Nosotros configuramos el bot desde los datos de tu negocio y lo dejamos hablando como tu equipo.

**7. "Mandame info por correo / WhatsApp y lo reviso."**
> Te envío un resumen de una página con el plan y un enlace a la demo de 3 minutos. ¿Te escribo el jueves para saber qué te pareció?
Registrar seguimiento en día 3 (sección 5).

**8. "No tengo tiempo para configurar nada."**
> No tienes que configurar nada. Con tus datos básicos (servicios, precios, horarios, dirección) armamos el bot nosotros. Tú solo revisas y apruebas.

**9. "Mis clientes no usan WhatsApp para pedir citas."**
> Entonces lo probamos con una semana de piloto. Si el número de mensajes es bajo, no tiene sentido que pagues. Revisamos los datos juntos al final.

**10. "Y si el bot se equivoca con una cita?"**
> El bot agenda en tu Google Calendar y puedes ver y cambiar cada cita. Si hay un conflicto de horario, no confirma y te avisa. Además, cualquier cambio se puede corregir desde el panel.

**11. "¿Esto no es para empresas grandes?"**
> Lo diseñamos para negocios como el tuyo en Cali: consultorios, talleres, restaurantes. Empezamos con un plan pequeño y subimos solo si te sirve.

**12. "Déjame pensarlo / hablo con mi socio."**
> Claro. ¿Qué información le llevarías para decidir? Te preparo un resumen con el plan, la demo y las respuestas a lo que más pregunta. ¿Lo agendamos con los dos en 15 minutos?

---

## 5. Secuencia de seguimiento

Solo si el lead no ha respondido o pidió pensarlo. Parar la secuencia en cuanto responda o diga NO.

**Día 1 - Recordatorio de valor (WhatsApp)**
> Hola [NOMBRE], te escribí ayer sobre la prueba de recepcionista con IA para [NEGOCIO]. Te dejo un video de 2 minutos con cómo responde a un paciente: [LINK_DEMO]. ¿Te sirve que lo veamos 10 minutos?

**Día 3 - Caso práctico (WhatsApp)**
> Hola [NOMBRE], un dato: [DATO_PILOTO, ej. "una clínica de Cali recuperó X citas en el primer mes"]. Si quieres, te calculo cuántas citas podrías recuperar con tus números. ¿Te lo preparo?

**Día 7 - Cierre con opción de salida (WhatsApp)**
> Hola [NOMBRE], última nota de mi parte. Si en este momento no es prioridad, responde NO y no te escribo más. Si quieres avanzar, dime un horario y lo agendamos esta semana.

Reglas:
- Máximo 3 mensajes en 7 días. No llamar después del día 7 sin respuesta.
- Si responden NO o "no me escriba", marcar como no contactar y respetar.
- Registrar cada toque en el CRM con fecha y canal.

---

## 6. Checklist para el equipo de desarrollo (para que el guion funcione en producto)

- La fábrica de bots debe poder generar un bot demo a partir de nombre, servicios, precios, horarios y dirección en menos de 10 minutos.
- El bot debe rechazar temas fuera del negocio con una respuesta fija de redirección (se usa en demo, paso 4).
- Escalamiento a humano: notificación al dueño por WhatsApp o panel, con botón para tomar la conversación.
- Panel: vista de conversaciones, citas y leads en español, sin datos de otros tenants.
- Registro de consentimiento de tratamiento de datos (Ley 1581) disponible para descargar por cliente.
- Opción de salida en mensajes de prospección (texto "responde NO") registrada en la base de leads.
- Plantilla de propuesta con `[PRECIO_PLAN]` leído desde la configuración de planes, no escrito a mano.
