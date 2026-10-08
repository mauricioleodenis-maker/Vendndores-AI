# 09 - Referencia tecnica: Google Places API (New)

Uso previsto: lead finder de Vendedores AI (Cali, Colombia). Buscar negocios por nicho y zona, obtener telefono, web, rating y resenas para el scoring y el pitch de outreach.

> Nota de verificacion: los nombres de campos y endpoints son los de Places API (New) segun la documentacion conocida. Los precios y cuotas cambian: confirmar en https://mapsplatform.google.com/pricing y en la consola (Quotas) antes de presupuestar. Las reglas de ToS se resumen para diseno; no son asesoria legal.

## 1. Autenticacion y headers comunes

- Base URL: `https://places.googleapis.com/v1`
- Header de API key: `X-Goog-Api-Key: <KEY>` (guardar en variable de entorno, nunca en el repo).
- Header de field mask OBLIGATORIO en Text Search y recomendado en Details: `X-Goog-FieldMask`.
- Si no se envia field mask, Text Search responde error; Details devuelve solo `id`/`name` por defecto.
- Restringir la key por API (Places API New) y por IP/referrer del backend.

## 2. Text Search (New)

- Metodo: `POST https://places.googleapis.com/v1/places:searchText`
- Headers: `Content-Type: application/json`, `X-Goog-Api-Key`, `X-Goog-FieldMask`.

Body minimo:
```json
{
  "textQuery": "dentista en Cali, Colombia",
  "languageCode": "es",
  "regionCode": "CO",
  "maxResultCount": 20,
  "locationBias": {
    "circle": { "center": { "latitude": 3.4516, "longitude": -76.5320 }, "radius": 5000.0 }
  }
}
```

Parametros utiles:
- `textQuery` (obligatorio): texto libre, p. ej. "clinica estetica Cali".
- `maxResultCount`: 1 a 20 por pagina.
- `pageToken`: para paginar (usar `nextPageToken` de la respuesta). Hay retraso breve antes de que el token sea valido; reintentar con backoff.
- `locationBias` (sugiere zona, no restringe) o `locationRestriction` (rectangulo estricto, solo `rectangle`).
- `includedType`: tipo de lugar (p. ej. `dentist`, `beauty_salon`, `car_repair`, `restaurant`). Usar `strictTypeFiltering: true` para excluir otros tipos.
- `openNow`, `minRating`, `priceLevels`: filtros opcionales.

Field mask para Text Search (prefijo `places.`):
```
X-Goog-FieldMask: places.id,places.displayName,places.formattedAddress,places.location,places.types,places.primaryType,places.businessStatus,places.googleMapsUri,nextPageToken
```
Agregar `places.nationalPhoneNumber,places.websiteUri,places.rating,places.userRatingCount` si se quiere evitar una llamada Details por lugar (sube el tier de precio; ver seccion 5).

Respuesta (forma):
```json
{ "places": [ { "id": "ChIJ...", "displayName": { "text": "...", "languageCode": "es" }, "formattedAddress": "..." } ], "nextPageToken": "..." }
```
Ojo: `displayName` es objeto; usar `displayName.text`.

## 3. Place Details (New)

- Metodo: `GET https://places.googleapis.com/v1/places/{PLACE_ID}`
- Header: `X-Goog-FieldMask: <campos>` (sin prefijo `places.`).
- Parametro opcional: `languageCode=es`, `regionCode=CO`, `sessionToken` (agrupar Autocomplete + Details; no aplica aqui).

Campos de contacto y calidad:

| Campo | Tipo | Notas |
|---|---|---|
| `id` | string | Place ID. Estable; se puede guardar indefinidamente. |
| `displayName.text` | string | Nombre comercial. |
| `formattedAddress` | string | Direccion completa. |
| `location.latitude/longitude` | number | Coordenadas. |
| `nationalPhoneNumber` | string | Formato local, p. ej. `312 345 6789`. |
| `internationalPhoneNumber` | string | Formato E.164-like, p. ej. `+57 312 345 6789`. Preferir este para normalizar. |
| `websiteUri` | string | Web del negocio (puede faltar). |
| `rating` | number | 1.0 a 5.0. Puede faltar si no hay resenas. |
| `userRatingCount` | int | Numero de resenas. |
| `reviews` | array | Hasta 5 resenas (las mas relevantes). Solo en Details. |
| `reviews[].rating`, `reviews[].text.text`, `reviews[].authorAttribution.displayName`, `reviews[].publishTime` | - | Estructura de cada resena. |
| `regularOpeningHours` | object | Horarios; `weekdayDescriptions` en texto. |
| `businessStatus` | enum | `OPERATIONAL`, `CLOSED_TEMPORARILY`, `CLOSED_PERMANENTLY`. Filtrar cerrados. |
| `types`, `primaryType`, `primaryTypeDisplayName.text` | - | Categoria para segmentar nicho. |
| `googleMapsUri` | string | Enlace a Maps. |
| `priceLevel` | enum | `PRICE_LEVEL_INEXPENSIVE` ... (poco fiable en Colombia). |
| `editorialSummary.text` | string | Resumen editorial (si existe). |

Field mask ejemplo para Details de lead (no pedir reviews si no se usan):
```
X-Goog-FieldMask: id,displayName,formattedAddress,internationalPhoneNumber,nationalPhoneNumber,websiteUri,rating,userRatingCount,businessStatus,primaryType,types,googleMapsUri,location
```
Con reviews (SKU mas caro, ver seccion 5):
```
X-Goog-FieldMask: id,reviews,rating,userRatingCount
```

Regla: pedir solo los campos necesarios. El precio sube con el campo de mayor tier del mask, no con el numero de campos.

## 4. Errores frecuentes

- `400 INVALID_ARGUMENT` con mask ausente: falta `X-Goog-FieldMask` en Text Search.
- `403 PERMISSION_DENIED`: API no habilitada en el proyecto, key restringida a otra API, o billing desactivado.
- `429 RESOURCE_EXHAUSTED`: cuota por minuto. Backoff exponencial con jitter.
- `NOT_FOUND` en Details: place_id obsoleto. Re-buscar por texto y actualizar el id.
- Respuesta vacia (sin `places`): no es error; manejar lista vacia.

## 5. Precios y SKUs (orientativo, verificar)

Google factura por SKU segun los campos pedidos. Estructura conocida (Places API New):
- **Text Search** y **Place Details** tienen tiers: Essentials (IDs y campos basicos), Pro, Enterprise (incluye telefono, web, rating y cuenta de resenas), Enterprise + Atmosphere (incluye `reviews`, `editorialSummary`, etc.).
- Cada tier tiene precio por 1.000 solicitudes y cuota gratuita mensual por SKU (el antiguo credito de 200 USD/mes se reemplazo por cuotas gratuitas por SKU; confirmar).
- Orden de magnitud (USD por 1.000 llamadas, referencia 2025): Pro del orden de 30 a 35; Enterprise del orden de 35 a 40 para Text Search; Details Enterprise + Atmosphere mas caro. Usar la calculadora oficial antes de decidir.

Estrategia de costo recomendada:
1. Text Search con mask de tier bajo (IDs, nombre, direccion, location) para listar candidatos.
2. Deduplicar por `id` antes de cualquier Details (cachear).
3. Details solo para lugares que pasan filtro (`businessStatus=OPERATIONAL`, nicho correcto).
4. Reviews solo si el scoring lo necesita; es el campo mas caro.
5. Llevar contador de llamadas por tenant y por SKU; poner tope mensual en la consola (budget alert + cuota).

## 6. Quotas y limites

- Cuotas por proyecto: solicitudes por minuto por API y por proyecto, ajustables en la consola (IAM y administracion > Cuotas). Valores exactos: confirmar en la consola; no asumir.
- Diseno: cola con rate limit por tenant, reintentos con backoff en 429/503, timeout de 10 s por request.
- Paginacion: 20 resultados maximos por pagina; traer varias paginas aumenta costo lineal.
- Text Search devuelve resultados limitados por consulta: para cubrir una zona grande, partir en celdas (grid de `locationBias` o `locationRestriction`) y deduplicar.

## 7. ToS: almacenamiento, cache y uso (resumen de diseno)

Resumen de los Service Specific Terms de Google Maps Platform (verificar version vigente):
- **place_id**: puede almacenarse de forma indefinida. Es la clave preferida para re-consultar.
- **Otro contenido de Places** (nombre, direccion, telefono, web, rating, resenas, fotos, coordenadas): cache limitado (historicamente 30 dias consecutivos) y luego refrescar o borrar. Confirmar el plazo vigente.
- **Resenas y texto de usuarios**: no mostrarlas como propias; si se muestran, respetar atribucion y reglas de display. Preferir no almacenarlas: guardar solo rating y conteo.
- **Atribucion**: mostrar "Google" / logo y fuente cuando se muestren datos de Places en pantallas de la app.
- **Prohibido** (en general): crear una base de datos alternativa o un servicio que replique Google Maps con datos de Places; usar el contenido para entrenar modelos sin permiso; exportar masivamente el catalogo; eludir limites de cache. Revisar la clausula vigente sobre usos de marketing o contacto antes de hacer outreach masivo con estos datos.
- **Coordenadas (lat/lng)**: tratar como contenido de Places (mismo plazo de cache).

Implicaciones para Vendedores AI:
- Guardar `google_place_id`, y refrescar nombre/telefono/web con TTL de 30 dias como maximo.
- No guardar resenas completas; guardar `rating` y `userRatingCount` con fecha de captura.
- Los telefonos y nombres de personas (dueños, contacto) son datos personales en Colombia (Ley 1581 de 2012): base legal y aviso antes de contacto masivo. Consultar abogado.
- El outreach por WhatsApp tiene sus propias reglas (Meta/WhatsApp Business); no es una regla de Google.

## 8. Mapeo para exportaciones CSV de scrapers de Google Maps

Columnas frecuentes en exportes de herramientas de terceros (Outscraper, Apify Google Maps scrapers, extensiones de Maps). Los nombres varian por herramienta; el importador debe aceptar alias y normalizar.

| Campo interno (propuesto) | Alias frecuentes en CSV | Transformacion |
|---|---|---|
| `business_name` | `name`, `title`, `business_name` | trim; requerido |
| `address` | `address`, `full_address`, `street` | trim |
| `city` | `city` | default "Cali" si vacio |
| `phone_raw` | `phone`, `phone_number`, `phone_1`, `phone1`, `telephone` | conservar original |
| `phone_e164` | derivado de `phone_raw` | solo digitos; si 10 digitos empieza en 3 (movil CO) -> `+57` + numero; si ya empieza con 57 -> `+57...`; validar longitud |
| `website` | `website`, `site`, `website_url`, `domain` | normalizar https://; quitar parametros de tracking |
| `rating` | `rating`, `stars`, `average_rating` | float; coma decimal -> punto |
| `review_count` | `reviews`, `reviews_count`, `user_ratings_total`, `review_count` | int; quitar separadores de miles |
| `category` | `category`, `type`, `categories`, `main_category` | si es lista separada por `;` o `,`, tomar la primera como principal y guardar el resto |
| `google_place_id` | `place_id`, `placeId`, `google_id`, `cid` | si es `cid` (no place_id), marcar `source_id_type=cid` y no mezclar con place_id |
| `latitude` | `latitude`, `lat` | float |
| `longitude` | `longitude`, `lng`, `long` | float |
| `google_maps_url` | `google_maps_url`, `link`, `url`, `maps_url` | guardar; no confundir con `website` |
| `business_status` | `business_status`, `status`, `permanently_closed` | descartar cerrados |
| `source` | (constante) | `csv_import:<herramienta>` |
| `imported_at` | (constante) | timestamp UTC |

Reglas de importacion:
- Encoding UTF-8 con BOM posible; separador `,` o `;` (detectar). Acentos en nombres: conservar.
- Deduplicar por `google_place_id` si existe; si no, por (`business_name` normalizado + `phone_e164`).
- Filas sin telefono y sin web: importar pero marcar `contactable=false`.
- Registrar fila de origen y errores en un reporte para el usuario (no fallar todo el lote).
- Guardar la fecha de captura: aplica el mismo limite de cache de la seccion 7.

## 9. Recomendacion para el plan

- Preferir la API oficial para el lead finder; usar CSV solo como importacion puntual con revision de ToS por parte del equipo.
- Definir en el modelo de datos: `google_place_id` (unico, indefinido), `captured_at`, `ttl_until` (captured_at + 30 dias), y campos de contacto con `contactable`.
- Crear un job de refresco que vuelva a llamar Details antes de `ttl_until` y elimine datos vencidos.
- Presupuesto: limitar llamadas por tenant y por mes; alerta en consola de facturacion.
- Pruebas: mockear las respuestas de Text Search y Details con fixtures JSON (sin red) en pytest.
