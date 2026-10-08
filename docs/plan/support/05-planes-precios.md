# 05 - Planes, precios y garantia (soporte)

Estado: propuesta de soporte para docs/plan/01 (seed de `plans`) y para la pantalla de venta. Valores en COP sin decimales. Los limites marcados "PROPUESTA" hay que validarlos con costo LLM real (`llm_usage`) antes de publicar. Ningun precio o cifra de resultado se muestra en el bot a clientes finales.

## 1. Precios (COP, IVA incluido si aplica: validar con contador)

| Plan | Code | Setup unico | Mensual | Ideal para |
|---|---|---|---|---|
| Basico | `basico` | 800.000 | 250.000 | Consultorio o taller de 1 sede, 1 persona en recepcion |
| Pro | `pro` | 1.200.000 | 400.000 | Clinica o taller con varios servicios y agenda compartida |
| Premium | `premium` | 2.000.000 | 600.000 | Restaurante con reservas o clinica multi-sede, mas volumen |

Seed (coincide con docs/plan/01): Basico 800k/250k, Pro 1.2M/400k, Premium 2M/600k.

Precio fundador (oferta de lanzamiento, PROPUESTA):
- Primeros 10 clientes, cualquier plan: setup con 50% de descuento, mensual congelado 6 meses al precio de lanzamiento.
- Ejemplo Basico fundador: setup 400.000 + 250.000/mes los meses 1-6.
- Condicion: contrato de 6 meses minimo y testimonio/caso de uso autorizado por escrito. Cupo limitado, se registra en `subscriptions.notes` con etiqueta `fundador`.
- Un cliente fundador no puede acumular otro descuento.

Cobro: setup al firmar; mensual por adelantado el dia de activacion (ciclo mensual, `current_period_end`). Sin cobro de fraccion: si activan el dia 20, el primer mes va completo.

## 2. Limites y funciones por plan

| Concepto | Basico | Pro | Premium | Regla |
|---|---|---|---|---|
| Conversaciones / mes (contacto unico por 24 h = 1) | 300 | 1.000 | 3.000 | PROPUESTA. Tope duro en `usage_counters`; al 80% aviso al admin; al 100% el bot responde mensaje de handoff, no corta al cliente |
| Calendarios de Google conectados | 1 | 2 | 5 | `max_calendars` |
| Sedes / recursos agendables | 1 | 2 | 5 | Ligado a `resource_id` si hay varios |
| Servicios en catalogo | 10 | 30 | Ilimitados | `max_services` |
| Usuarios del panel | 2 (owner + 1 operator) | 5 | 15 | Roles owner/admin/operator |
| Recordatorios de cita | 24 h | 24 h + 2 h | 24 h + 2 h + personalizados | `reminders` en limits |
| Seguimiento de no-show / reactivacion | No | Si (1 secuencia) | Si (varias secuencias) | `followups_enabled` |
| Plantillas WhatsApp aprobadas | 3 | 10 | 25 | Fuera de ventana 24 h solo plantillas aprobadas |
| Bot factory (generacion de config con IA) | 1 generacion + ajustes | Ilimitadas razonables | Ilimitadas razonables | "Razonable": 5 regeneraciones completas por mes |
| Pruebas en sandbox | Si | Si | Si | `/admin/bots/{id}/probar` |
| Voz (llamadas) | No | No | No al lanzamiento | `voice_enabled` = false hasta que exista canal voz |
| Minutos de voz incluidos | 0 | 0 | 0 (se incluyen cuando salga voz: PROPUESTA 100 min/mes) | Voz no se vende antes de estar disponible |
| Soporte | Correo, 2 dias habiles | WhatsApp, 1 dia habil | WhatsApp, mismo dia habil | |
| Reportes | Resumen mensual basico | Panel de citas y conversaciones | Panel + exportacion CSV (con sanitizacion anti CSV-injection) | |
| Retencion de mensajes crudos | 30 dias | 30 dias | 30 dias | Minimizacion Ley 1581; datos de salud no se guardan como diagnostico |

Reglas de exceso:
- No hay cobro por exceso automatico al lanzamiento. Al superar conversaciones, el bot pasa a handoff y el admin recibe aviso; se ofrece upgrade.
- Un cliente que supera el tope 2 meses seguidos recibe propuesta de cambio de plan, no cobro sorpresa.

Limites que nunca se prometen en venta: tiempos de respuesta del LLM, resultados de conversion, volumen de citas generadas.

## 3. Garantia de 30 dias (terminos propuestos)

Alcance: aplica al setup y a la primera mensualidad, por cliente, una sola vez.

Condiciones:
1. Reembolso total del setup y de la primera mensualidad si, dentro de los 30 dias calendario desde la activacion, el cliente solicita cancelacion por escrito y el bot no quedo publicado o no funciona de forma basica (no responde, no agenda, no respeta el alcance acordado).
2. Si el bot funciona pero el cliente no esta satisfecho, la garantia cubre ajustes: hasta 3 rondas de ajuste de tono, servicios, horarios y respuestas sin costo.
3. Requisitos del cliente: entregar informacion del negocio (horarios, servicios, precios), conectar su calendario y su numero de WhatsApp en el plazo acordado. Si el cliente no entrega informacion, el plazo de 30 dias no corre.
4. No aplica a: clientes que dieron de baja datos sensibles sin consentimiento, uso del bot fuera del alcance (diagnosticos, promesas de precio o resultados), ni a fallas de terceros (Twilio, Google, Meta) fuera de nuestro control; en esos casos se ayuda a reconfigurar.
5. Reembolso por el mismo medio de pago en maximo 15 dias habiles tras la aprobacion.
6. Derecho de retracto y garantias legales: validar con abogado frente a Ley 1480 de 2011 (Estatuto del Consumidor). La garantia comercial no reemplaza derechos legales irrenunciables.
7. Contrato: estas condiciones deben estar en la propuesta firmada y en `/privacidad` o pagina de terminos.

## 4. Datos para la app (YAML cargable)

```yaml
# Bloque tomado de este archivo; copiar a docs/plan/support/05-planes-precios.yaml al implementar (seed en app/cli.py, idempotente)
currency: COP
vat_included: unknown            # validar con contador
plans:
  - code: basico
    name: Basico
    setup_fee_cop: 800000
    monthly_fee_cop: 250000
    limits:
      max_conversations_month: 300
      max_calendars: 1
      max_resources: 1
      max_services: 10
      max_panel_users: 2
      max_whatsapp_templates: 3
      max_bot_regenerations_month: 5
      reminders: [24h]
      followups_enabled: false
      followup_sequences: 0
      voice_enabled: false
      voice_minutes_month: 0
      sandbox_enabled: true
      export_csv: false
      support: {channel: email, sla_business_days: 2}
  - code: pro
    name: Pro
    setup_fee_cop: 1200000
    monthly_fee_cop: 400000
    limits:
      max_conversations_month: 1000
      max_calendars: 2
      max_resources: 2
      max_services: 30
      max_panel_users: 5
      max_whatsapp_templates: 10
      max_bot_regenerations_month: 5
      reminders: [24h, 2h]
      followups_enabled: true
      followup_sequences: 1
      voice_enabled: false
      voice_minutes_month: 0
      sandbox_enabled: true
      export_csv: false
      support: {channel: whatsapp, sla_business_days: 1}
  - code: premium
    name: Premium
    setup_fee_cop: 2000000
    monthly_fee_cop: 600000
    limits:
      max_conversations_month: 3000
      max_calendars: 5
      max_resources: 5
      max_services: null           # ilimitados
      max_panel_users: 15
      max_whatsapp_templates: 25
      max_bot_regenerations_month: 5
      reminders: [24h, 2h, custom]
      followups_enabled: true
      followup_sequences: 3
      voice_enabled: false         # activar al lanzar voz
      voice_minutes_month: 0       # PROPUESTA 100 al lanzar voz
      sandbox_enabled: true
      export_csv: true
      support: {channel: whatsapp, sla_business_days: 0}
founder_offer:
  enabled: true
  slots: 10
  setup_discount_pct: 50
  monthly_freeze_months: 6
  min_contract_months: 6
  tag: fundador
  stackable: false
  requires: [written_case_study_authorization]
guarantee:
  days: 30
  scope: [setup, first_monthly]
  once_per_client: true
  refund_channel: original_payment_method
  refund_max_business_days: 15
  adjustment_rounds: 3
  excluded:
    - out_of_scope_use          # diagnosticos, precios o resultados garantizados
    - third_party_outage        # Twilio, Google, Meta
    - missing_client_inputs     # el plazo no corre sin datos del cliente
  legal_review_required: true   # Ley 1480 de 2011
overage_policy:
  auto_charge: false
  on_limit: handoff_and_notify_admin
  upgrade_prompt_after_months: 2
  forbidden_promises: [resultados, conversion, volumen_citas, tiempos_respuesta_llm]
```

## 5. Pendientes para validar

- Confirmar limites con datos de costo LLM por conversacion (`llm_usage`) antes de fijar 300/1.000/3.000.
- Decidir si IVA va incluido o se suma (`vat_included`).
- Revisar garantia y retracto con abogado (Ley 1480 de 2011, Ley 1581 de 2012 para datos).
- Definir precio y limites de voz cuando salga el canal.
- Cupo fundador: confirmar 10 slots y si el descuento de setup aplica tambien a Premium.
