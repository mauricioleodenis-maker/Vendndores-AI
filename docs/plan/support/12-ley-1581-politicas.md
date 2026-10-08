# 12 - Textos Ley 1581 de 2012 (borradores en español)

> **BORRADOR PARA REVISIÓN DE ABOGADO.** Ningún texto de este documento puede publicarse, enviarse a clientes ni incorporarse a un contrato sin revisión por abogado especializado en habeas data (Ley 1581 de 2012, Decreto 1074 de 2015 Título 2 Capítulo 25, Decreto 1377 de 2013). Los artículos citados deben verificarse. Los campos entre `[corchetes]` son variables a completar.

Glosario de roles (ver `docs/plan/01-arquitectura-datos.md` sección 9):
- **Agencia** ("[NOMBRE AGENCIA]", NIT [NIT]): Responsable de los datos de leads B2B y de los datos de su propia operación. **Encargada** del tratamiento de los datos de pacientes/clientes finales que el bot de cada clínica o negocio procesa.
- **Cliente / Negocio** (dentista, clínica estética, taller, restaurante): **Responsable** del tratamiento frente a sus pacientes/clientes.
- **Titular**: la persona natural cuyos datos se tratan.

---

## 1. Política de tratamiento de datos personales de la Agencia (versión pública)

**1.1 Identificación del responsable y encargado.**
[NOMBRE AGENCIA], NIT [NIT], domicilio en Cali, Valle del Cauca, Colombia. Correo para ejercer derechos: [CORREO_DPO]. Teléfono: [TELEFONO].

**1.2 Alcance.** Esta política aplica a los datos personales que la Agencia recolecta y trata en: (a) su sitio web y formularios; (b) la búsqueda, contacto y seguimiento de negocios potenciales (leads); (c) los asistentes virtuales (bots de WhatsApp y, en el futuro, voz) que la Agencia opera por cuenta de sus clientes; (d) la facturación y relación comercial con clientes.

**1.3 Finalidades - leads (Agencia como Responsable).**
- Contactar a representantes de negocios para ofrecer el servicio de asistente virtual, con base en interés legítimo comercial sobre datos públicos de la empresa.
- Gestionar el historial de contacto y respetar las solicitudes de supresión.
No se venden datos personales a terceros.

**1.4 Finalidades - clientes del bot (Agencia como Encargada).** La Agencia trata los datos de pacientes y clientes finales únicamente para ejecutar las instrucciones del Responsable (el negocio cliente): gestionar citas, enviar recordatorios autorizados y responder consultas sobre servicios, precios y horarios. La Agencia no usa esos datos para finalidades propias, publicidad o creación de bases de datos ajenas al servicio contratado.

**1.5 Datos sensibles.** Los datos relativos a la salud son sensibles (art. 5 Ley 1581). La Agencia no solicita, no almacena ni infiere diagnósticos, síntomas, historias clínicas ni tratamientos. Si un titular los escribe, el sistema los enmascara antes de cualquier registro de analítica y el bot deriva el caso a un humano. Ningún dato sensible es obligatorio para el titular.

**1.6 Derechos del titular.** Conocer, actualizar, rectificar y suprimir sus datos; revocar la autorización; solicitar prueba de la autorización; presentar quejas ante la Superintendencia de Industria y Comercio (SIC) (art. 8 Ley 1581). Para pacientes de un negocio, la solicitud se dirige al negocio (Responsable) o a la Agencia, que la trasladará en un plazo máximo de [2] días hábiles. Consultas: [10] días hábiles; reclamos: [15] días hábiles, prorrogables según la ley.

**1.7 Procedimiento.** Solicitud escrita o por WhatsApp con la palabra clave "BORRAR MIS DATOS" o "STOP" (supresión de recordatorios y marketing). Verificación de identidad mediante el número de teléfono desde el que se escribe. Respuesta por el mismo canal.

**1.8 Transferencia internacional.** Para operar el servicio, algunos datos se procesan fuera de Colombia por proveedores de: (i) inteligencia artificial (Anthropic, EE. UU.); (ii) mensajería (Twilio, EE. UU.); (iii) calendario (Google LLC, EE. UU.); (iv) búsqueda de negocios (Google Places). Se envía solo el contexto mínimo necesario, con alias en lugar de nombre completo y sin número telefónico cuando es posible. [Confirmar base legal de la transferencia según art. 26 Ley 1581 y su reglamentación; verificar contratos de procesamiento de cada proveedor y configuración de retención cero.]

**1.9 Seguridad y retención.** Datos de contacto cifrados en reposo; mensajes cifrados con retención limitada a [N] meses o hasta la supresión; registros de auditoría de accesos. Los datos se conservan mientras dure la relación y las obligaciones legales aplicables.

**1.10 Vigencia.** Política v[1.0], vigente desde [FECHA]. Cualquier cambio sustancial se comunica con [10] días de antelación.

---

## 2. Aviso de privacidad corto (primer mensaje del bot)

Se envía en el primer mensaje de cada contacto nuevo. Máximo 3 líneas en WhatsApp.

**Versión para el bot (atención, recordatorios y datos de salud):**
> Hola, soy el asistente virtual de *[NOMBRE_NEGOCIO]*. Para gestionar tus citas tratamos tu nombre y teléfono. Te pedimos *no* compartir datos de salud por este chat. Responsable: [NOMBRE_NEGOCIO]; el servicio lo opera [NOMBRE AGENCIA]. Política de privacidad: [URL_CORTA]. Responde *SI* para continuar, o *STOP* para no recibir recordatorios.

**Versión para recordatorios (se pide al reservar, opt-in separado):**
> ¿Autorizas que te enviemos recordatorios de tu cita por WhatsApp? Responde *SI* o *NO*. Puedes cambiar de opinión escribiendo *STOP*.

**Versión para marketing (solo si el negocio lo activa, opt-in separado):**
> ¿Quieres recibir promociones y novedades de *[NOMBRE_NEGOCIO]* por WhatsApp? Responde *SI* para aceptar. *NO* no afecta tu cita.

**Respuesta de confirmación (tras "SI" al aviso):**
> Gracias. Usaremos tus datos solo para gestionar tu atención. Para ejercer tus derechos escribe *DERECHOS* o visita [URL_CORTA].

**Respuesta a "STOP":**
> Listo, no recibirás recordatorios ni promociones. Tus citas confirmadas siguen vigentes. Si quieres borrar tus datos escribe *BORRAR MIS DATOS*.

**Reglas de implementación:** el aviso se registra en `consents` con `policy_version`, `granted_at`, `channel` y texto exacto enviado. Sin "SI" explícito no se envían recordatorios de cita fuera de la ventana de servicio. Nunca se infiere marketing.

---

## 3. Texto de consentimiento para datos sensibles (salud)

Este consentimiento aplica solo si el negocio decide permitir que el bot reciba información de salud mencionada espontáneamente. Por defecto el bot **no solicita** datos de salud.

**Texto propuesto:**
> Entiendo que la información sobre mi salud es un dato sensible. No estoy obligado a suministrarla. Si la comparto por este medio, autorizo a *[NOMBRE_NEGOCIO]* a usarla únicamente para orientarme sobre la atención solicitada y a no almacenarla en registros de analítica. Sé que el asistente virtual no emite diagnósticos ni recomendaciones médicas y que, para cualquier consulta clínica, debo acudir al profesional de salud.
>
> [ ] Acepto que mis datos de salud sean tratados con esta finalidad.
> *(Sin esta casilla, el bot responde sin registrar el contenido clínico y deriva a un humano.)*

**Notas para abogado:** la autorización debe ser expresa, previa e informada (arts. 6 y 9 Ley 1581). Verificar si el consentimiento por WhatsApp (respuesta afirmativa) es suficiente como prueba o si se requiere enlace a formulario firmado para el Responsable.

---

## 4. Cláusulas de encargo del tratamiento (DPA) - Agencia y clínica

Documento: **Acuerdo de Transmisión de Datos Personales**, anexo al contrato de servicio. Versión [1.0]. Se acepta en el onboarding (guardar `tenants.dpa_version` y `tenants.dpa_accepted_at`).

**Cláusula 1 - Partes y roles.** El CLIENTE (Responsable) determina las finalidades del tratamiento de los datos de sus pacientes y clientes. La AGENCIA (Encargada) trata esos datos por cuenta del CLIENTE, exclusivamente para prestar el servicio contratado.

**Cláusula 2 - Objeto y duración.** Operación del asistente virtual de WhatsApp (y voz cuando se contrate), gestión de citas, recordatorios autorizados y reportes. Duración: vigencia del contrato más [N] días para la supresión final.

**Cláusula 3 - Categorías de datos.** Identificación de contacto (nombre, teléfono), datos de la cita (servicio, fecha, hora), contenido de mensajes. Datos sensibles: no se solicitan; si se mencionan, se aplica el protocolo de la cláusula 6.

**Cláusula 4 - Obligaciones de la AGENCIA como Encargada.** (a) Tratar los datos solo según instrucciones documentadas del CLIENTE; (b) no usar los datos para finalidades propias ni para entrenar modelos de terceros; (c) garantizar que el personal y los proveedores con acceso estén obligados a confidencialidad; (d) implementar medidas de seguridad (cifrado en tránsito y reposo, control de acceso por rol, registro de auditoría); (e) permitir la verificación de cumplimiento razonable, con aviso previo de [10] días hábiles; (f) actualizar o suprimir datos cuando el CLIENTE lo instruya o cuando reciba una solicitud de titular, en el plazo de [2] días hábiles; (g) notificar incidentes de seguridad al CLIENTE sin dilación indebida y, en todo caso, en [48] horas desde su conocimiento; (h) informar a la SIC si lo exige la ley.

**Cláusula 5 - Subencargados.** El CLIENTE autoriza a la AGENCIA a usar los subencargados listados en el Anexo A (Anthropic, Twilio, Google, [hosting/base de datos]). La AGENCIA informará cambios con [15] días de antelación y el CLIENTE podrá oponerse dentro de ese plazo; en ese caso, las partes evaluarán la terminación sin penalidad. Cada subencargado queda sujeto a obligaciones equivalentes.

**Cláusula 6 - Datos sensibles.** La AGENCIA no solicita datos de salud. Si el bot los recibe, el sistema los enmascara en logs y analítica, no los extrae a campos estructurados y, cuando aplique, deriva la conversación a un humano designado por el CLIENTE. El CLIENTE es responsable de la información que sus pacientes voluntariamente compartan fuera del protocolo.

**Cláusula 7 - Responsabilidades del CLIENTE.** (a) Garantizar que tiene autorización o base legal para los datos que entrega; (b) entregar el aviso de privacidad y gestionar las autorizaciones frente a sus pacientes (texto de la sección 2); (c) atender las solicitudes de los titulares o trasladarlas a la AGENCIA; (d) no instruir a la AGENCIA para tratamientos ilícitos.

**Cláusula 8 - Transferencia internacional.** El CLIENTE autoriza la transferencia de datos a los proveedores listados en el Anexo A, ubicados en EE. UU., bajo las condiciones de la sección 1.8 de la política. [Verificar cumplimiento de art. 26 Ley 1581.]

**Cláusula 9 - Retención y devolución.** Al terminar el contrato, la AGENCIA devuelve o suprime los datos en [30] días, salvo obligación legal de conservación. Copia de respaldo se purga en [90] días. Se emite certificado de supresión a solicitud del CLIENTE.

**Cláusula 10 - Responsabilidad e indemnidad.** Cada parte responde por los daños causados por el incumplimiento de sus obligaciones. [Definir tope de responsabilidad y excepciones; revisar con abogado.]

**Cláusula 11 - Ley aplicable.** Leyes de la República de Colombia. Competencia: [jueces de Cali / arbitraje]. Prevalencia: en caso de conflicto con el contrato de servicio, prevalece este acuerdo en lo relativo a datos personales.

**Cláusula 12 - Aceptación.** Aceptación digital en el panel de onboarding con casilla explícita, registro de usuario, fecha, IP y versión del texto.

---

## 5. Checklist de implementación (para builders)

- [ ] Versionar cada texto (`policy_version`); guardar texto exacto enviado en `consents.evidence`.
- [ ] Mostrar la política pública en URL estable: [URL_CORTA].
- [ ] Opt-in separado: `atencion` (primer mensaje), `recordatorios` (al reservar), `marketing` (solo SI explícito).
- [ ] Palabras clave: STOP, BORRAR MIS DATOS, DERECHOS; implementadas en el pipeline de entrada antes que el LLM.
- [ ] Redactor de datos sensibles antes de logs y analítica.
- [ ] DPA aceptado y versionado en `tenants` antes de publicar el bot.
- [ ] Prompt del bot sin solicitud de datos clínicos; pruebas en `tests/conversation/test_privacy.py`.

## 6. Preguntas abiertas para el abogado

1. ¿La Agencia debe inscribir sus bases de datos en el Registro Nacional de Bases de Datos (RNBD) de la SIC, según tamaño y activos? ¿Aplica a las bases de los clientes?
2. ¿Es válido el aviso y consentimiento por WhatsApp (respuesta "SI") como prueba suficiente, o se requiere autorización escrita o firma electrónica?
3. ¿Cuál es la base legal correcta para prospección B2B con teléfonos personales de representantes (interés legítimo, opt-out, o consentimiento)? Relacionar con la Circular SIC sobre marketing y con la regulación de WhatsApp Business.
4. ¿Qué transferencia internacional requiere autorización específica de la SIC o basta la cláusula contractual?
5. Tope de responsabilidad, indemnidad y seguros en la cláusula 10.
6. Plazos de retención de mensajes y mecanismo de purga.
7. Tratamiento de datos de menores (pacientes de odontopediatría) y de representantes legales.
