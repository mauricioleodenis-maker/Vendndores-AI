# Checklist Meta y Twilio

Úsalo antes de activar WhatsApp para un cliente y para la cuenta de la agencia.

## Cuenta y número

- [ ] Meta Business Manager verificado a nombre de la empresa.
- [ ] Número de WhatsApp Business dedicado, que no esté en la app personal.
- [ ] Nombre de perfil aprobado y descripción del negocio.
- [ ] Remitente de WhatsApp registrado en Twilio.
- [ ] Credenciales de Twilio solo en variables de entorno, nunca en el repositorio.

## Plantillas

- [ ] Nombre en minúsculas con guion bajo, único por cuenta.
- [ ] Categoría correcta: UTILITY para citas y recordatorios, MARKETING para reseñas y prospección.
- [ ] Ejemplos de variables representativos (sin ellos Meta rechaza).
- [ ] Sin variable al inicio ni al final del cuerpo.
- [ ] Sin acortadores de enlaces ni emojis en UTILITY.
- [ ] Sin datos de salud, motivo de consulta ni diagnóstico en el texto.
- [ ] Solo se usan plantillas en estado aprobado.
- [ ] Cambiar una plantilla aprobada implica crear una nueva.

## Reglas de envío

- [ ] Texto libre solo dentro de la ventana de 24 h desde el último mensaje del cliente.
- [ ] Fuera de la ventana, solo plantillas aprobadas.
- [ ] MARKETING solo a quien aceptó o escribió primero.
- [ ] Prospección en frío masiva por WhatsApp está prohibida: riesgo de bloqueo del número.
- [ ] STOP, BAJA, NO y CANCELAR marcan baja de inmediato.

## Configuración técnica

- [ ] Webhook de mensajes entrantes apuntando a la URL pública con HTTPS.
- [ ] Validación de firma de Twilio activa.
- [ ] Callback de estado activo (entregado, leído, fallido).
- [ ] Modo de prueba (dry-run) apagado solo cuando todo lo anterior esté listo.
- [ ] Prueba end-to-end con un número propio.

## Calidad del número

- [ ] Vigilar la calificación de calidad del número en Meta.
- [ ] Pausar campañas si suben las bajas o los bloqueos.
- [ ] Empezar con pilotos de 10 a 20 contactos.
