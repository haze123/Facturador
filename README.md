# Facturador

Daemon de Facturación Electrónica SUNAT (SFS v2.1).

Corre en segundo plano y emite comprobantes electrónicos a SUNAT sin intervención: lee los pendientes de la base de datos de la aplicación, genera los archivos que necesita el SFS (Sistema de Facturación SUNAT), los entrega al facturador local —que firma el XML y lo envía— y procesa las respuestas (CDR) de SUNAT para cerrar cada comprobante.

Emite facturas, notas de crédito y notas de débito de forma individual, y agrupa las boletas en un resumen diario.

Corre dos hilos en paralelo:
- **Hilo Generador**: cada `INTERVALO_GENERACION_SEG` segundos (60s por defecto) revisa si hay comprobantes por enviar.
- **Hilo CDR**: monitorea en tiempo real la carpeta `RPTA` y procesa el ZIP en cuanto SUNAT responde, con un barrido de respaldo cada `INTERVALO_BARRIDO_RPTA_SEG` segundos.

### El SFS solo lee DATA cuando se le pide

El SFS **no vigila la carpeta `DATA`**: la escanea al cargar su pantalla (`cargarArchivosContribuyente` cuelga de `CargarPantalla.htm`) o desde un job programado que exige tener el temporizador prendido. Ni `GenerarComprobante.htm` ni `enviarXML.htm` lo hacen — esos operan solo sobre lo que ya está registrado en su bandeja.

Por eso el daemon llama a `sincronizar_bandeja_sfs()` al inicio de cada ciclo y antes de entregar documentos nuevos. Sin eso dependería de que alguien tuviera la bandeja abierta en el navegador (donde la página refresca sola y de paso dispara el escaneo): con la ventana cerrada, los archivos se quedan en `DATA` sin que nadie los mire y **no se emite nada**.

## Tipos de comprobante

Se emiten factura (`01`), nota de crédito (`07`) y nota de débito (`08`) de forma individual, y boleta (`03`) agrupada en el resumen diario (`RC`, ver más abajo). La comunicación de baja (`RA`) todavía no.

Las notas exigen la referencia al documento que corrigen, y esos campos salen de `Factura`:

| Campo del SFS | Columna |
|---|---|
| `codMotivo` | `tipoNota` (catálogo 09 para NC, 10 para ND) |
| `desMotivo` | `motivoDocumentoAfectado` (si está vacío se usa la descripción del catálogo) |
| `tipDocAfectado` | `tipoDocumentoAfectado` |
| `numDocAfectado` | `numeracionDocumentoAfectado` |

Si a una nota le falta `tipoNota`, `tipoDocumentoAfectado` o `numeracionDocumentoAfectado`, **no se emite**: se registra un WARNING y se reintenta cuando alguien complete el dato. El código de motivo no se deduce ni se rellena por defecto, porque una nota con el motivo equivocado es una declaración incorrecta ante SUNAT.

## Validaciones previas

Antes de escribir cualquier archivo, todo comprobante (y cada boleta candidata a entrar al resumen diario) pasa por `_validar_campos_obligatorios()`: numeración, fecha de emisión válida y total. Los comprobantes individuales además necesitan al menos un ítem. Si falta algo, **no se genera nada** — se registra un WARNING con el detalle de qué falta y se reintenta en el próximo ciclo, cuando se corrija el dato en la BD. Antes de esto, una fecha ilegible terminaba como una excepción genérica en el log, sin decir qué comprobante era ni por qué.

No valida (todavía) dígito verificador de RUC/DNI, cuadre de totales, ni ítems con IGV=0 (ver "Limitaciones conocidas").

## Fecha de emisión y zona horaria

La aplicación guarda sus fechas con el reloj de su servidor de base de datos, que corre en **UTC**. SUNAT, en cambio, espera la fecha de emisión en la **hora local del emisor**. Sin corregir eso, toda venta hecha entre las 19:00 y la medianoche (Perú es UTC−5) queda registrada con la fecha del día siguiente, y a SUNAT se le declararía una fecha que todavía no llegó — que rechaza. En el resumen diario, además, esa boleta quedaría agrupada en el día equivocado.

Por eso el daemon **mide** el desfase en vez de asumirlo: al inicio de cada ciclo compara el reloj de la BD con la hora local y aplica esa corrección (`detectar_desfase_bd()` → `fecha_local()`). Si alguien cambia la zona horaria del servidor —a hora de Lima, por ejemplo— el daemon se ajusta solo en el ciclo siguiente y lo deja anotado en el log; un valor fijo, en cambio, habría quedado al revés del problema, corriendo las fechas para el otro lado sin que nadie se entere. Si la medición falla, se conserva la última corrección conocida en lugar de arriesgar una fecha equivocada.

Se puede forzar un valor con `DESFASE_BD_HORAS` (en horas) si hiciera falta.

## Archivos que genera en DATA

No son los mismos para todos los tipos, y equivocarse hace que el SFS rechace el documento. Esta tabla es para los comprobantes que se envían individualmente (factura y notas); la boleta no genera estos archivos — va agrupada en el resumen diario (`.RDI`/`.TRD`, ver más abajo).

| Archivo | Factura `01` | Notas `07` / `08` |
|---|:---:|:---:|
| `.cab` — cabecera, 18 columnas | ✓ | — |
| `.NOT` — cabecera de nota, 21 columnas | — | ✓ |
| `.det` — detalle, **36 columnas** | ✓ | ✓ |
| `.tri` — tributos | ✓ | ✓ |
| `.ley` — leyenda | ✓ | ✓ |
| `.PAG` — forma de pago | ✓ | — |

Tres reglas que no están documentadas por SUNAT y que se descubrieron probando contra el ambiente beta:

- **La cabecera de las notas va en `.NOT`, no en `.cab`.** El SFS identifica cada documento por la extensión de su archivo de cabecera. Con la cabecera en `.cab` responde *"El archivo no existe: ...NOT"* aunque el contenido sea correcto.
- **La forma de pago solo va en facturas.** En boletas, el validador de SUNAT lee `cac:PaymentTerms` como información de detracción y devuelve el error `3128`. En notas, ese nodo solo admite `Credito` o `Cuota*`, así que `Contado` devuelve el error `3246`.
- **El detalle siempre lleva 36 columnas.** El mensaje de error del SFS para la nota de débito dice *"(30 columnas)"*, pero su código compara contra 36. Seguir ese mensaje hace que rechace el archivo.

Los archivos se borran de `DATA` al final del ciclo en que el SFS cierra el documento (`IND_SITU` `03` o `04`). Los de comprobantes bloqueados o rechazados se conservan, porque sirven para diagnosticar.

## Estados y reintentos

El daemon se guía por el `IND_SITU` que el SFS lleva en su propia base:

| Estado | Significado | Qué hace el daemon |
|---|---|---|
| `03` / `04` | Aceptado (con o sin observaciones) | Cierra con `enviado=true` |
| `05` | Anulado | Lo reporta como `BLOQUEADO`; **no** lo reenvía |
| `06` | Con errores | Lo reporta como `BLOQUEADO`; no lo reenvía |
| `10` | Rechazado por SUNAT | Lo regenera y lo reenvía, hasta `MAX_REINTENTOS_RECHAZO` veces |

Cuando llega un CDR de rechazo, el motivo se guarda en `Factura.errors` (código y descripción de SUNAT, con fecha) además de quedar en el log, y el ZIP se archiva en `RPTA/errores/`. El comprobante nunca pasa a `enviado=true`.

### Tope de reenvíos

Un reenvío manda **exactamente los mismos datos**, así que si SUNAT rechazó por un dato mal armado el resultado no cambia. Por eso cada comprobante tiene un tope de reenvíos (3 por defecto); al agotarlo deja de reintentarse y pasa a reportarse como `BLOQUEADO` hasta que se corrija el dato.

El conteo se guarda en `reintentos.json` (no se versiona) para que un reinicio de PM2 no reinicie el ciclo:

```json
{ "F001-000107": { "tipo": "01", "intentos": 3, "ultimo": "2026-08-14 15:33:05",
                   "motivo": "2800 - El dato ingresado en el tipo de documento no es valido" } }
```

El contador se borra solo cuando llega el CDR de aceptación. Si corriges el dato y quieres reintentar antes de eso, elimina esa entrada del archivo.

### Recuperación tras un corte de conexión

Si el SFS alcanza a enviar un comprobante y la respuesta nunca vuelve —se corta internet, se cae el proceso—, nadie sabe si SUNAT lo llegó a registrar. Reenviarlo a ciegas arriesga duplicarlo, y un duplicado ante SUNAT solo se deshace con una nota de crédito.

Por eso, al inicio de cada ciclo, el daemon revisa los comprobantes que el SFS marcó como enviados (tienen fecha de envío en su base) pero que llevan más de `CONSULTA_SUNAT_TRAS_MIN` minutos sin CDR. Para esos, consulta directo a SUNAT por SOAP (`billConsultService`):

- Si SUNAT lo tiene registrado, descarga el CDR y lo deja en `RPTA` — el hilo CDR lo procesa igual que si hubiera llegado por el camino normal.
- Si confirma que no lo tiene, lo saca de la bandeja del SFS para que el próximo ciclo lo regenere y reenvíe.
- Ante cualquier otra respuesta —código desconocido, falla de red, credenciales ausentes— no toca nada. Ante la duda, nunca reenvía.

Requiere `SOL_USUARIO` y `SOL_CLAVE` en el `.env`; si faltan, este paso simplemente no corre y el resto del daemon sigue igual. El servicio de consulta de SUNAT **solo existe en producción** (no tiene variante beta), pero consultar es de solo lectura: no emite nada y no depende de a qué ambiente esté apuntando el SFS para enviar.

## Resumen diario de boletas

Ninguna boleta se envía individualmente. Todas se acumulan y salen agrupadas en un resumen diario (`RC`), un tipo de documento más para el SFS (mismos endpoints REST, mismo patrón de dos pasadas) que por debajo SUNAT procesa con un flujo de ticket en vez de respuesta inmediata.

Esto evita el problema de origen: una boleta enviada más de 5 días después de su emisión, SUNAT la rechaza para envío individual (`IND_SITU='06'`) y queda bloqueada para siempre. Agrupándolas en un resumen ese límite no aplica.

Cómo funciona, en cada ciclo:

1. `obtener_boletas_para_resumen()` junta las boletas con `enviado=false` y `fechaEmision` anterior al día de hoy —las de hoy se dejan para el resumen de un día siguiente—, salvo las que ya estén dentro de un resumen que el SFS todavía tiene en curso.
2. `generar_resumen_diario()` les asigna una numeración propia del daemon, `RC-YYYYMMDD-NNN` (correlativo persistido en `resumenes.json`, no se versiona), y escribe `.RDI` (una línea por boleta, 23 columnas) y `.TRD` (desglose de tributos por línea, 6 columnas) en `DATA`.
3. El resumen se entrega al SFS igual que cualquier otro documento (`activar_procesamiento_sfs()`).
3. SUNAT no devuelve el CDR en el acto como con una factura: responde un **ticket**. `recuperar_cdr_resumenes()` lo consulta por SOAP (`getStatus`) hasta que está listo y deja el CDR en `RPTA`.
4. Cuando llega el CDR, como un resumen no es una fila de `Factura` sino que agrupa muchas, el cierre hace un `UPDATE ... WHERE "numeracionComprobante" = ANY(...)` con la lista de boletas que se guardó en `resumenes.json` al generarlo, en vez del `UPDATE` de una sola fila que usa el resto de los tipos.
5. Un resumen rechazado se reintenta igual que cualquier otro documento (mismo tope `MAX_REINTENTOS_RECHAZO`); si se agota, queda `BLOQUEADO` y sus boletas no entran a un resumen nuevo hasta que se revise a mano.

No cubre el rechazo parcial de una sola boleta dentro de un resumen aceptado por SUNAT: ese caso queda para revisión manual, igual que cualquier otra situación que el daemon no puede resolver solo.

### Detalles del formato que costó descubrir

Todo esto salió de decompilar el SFS y de rechazos reales en homologación, no de la documentación:

- **El nombre de archivo va sin el `RC-` del id.** `validarNombreArchivo()` exige exactamente 4 tramos separados por guión (`RUC-TIPO-SERIE-NÚMERO`); con el prefijo quedaban 5 y **el SFS descartaba el archivo en silencio**, sin generar el XML ni dejar rastro en su bandeja ni en su log. En todo lo demás (bandeja, API REST, CDR) el id sí lleva el `RC-`.
- **`.RDI` no es una cabecera única**: es una línea de 23 columnas *por cada boleta*. El `.TRD` es el desglose de tributos, 6 columnas, vinculado por la posición de la línea.
- **`tipDocResumen` (columna 3) es el tipo de comprobante (`03`), no el estado de la línea.** Confundirlo da el error `2241`. El estado (`1` = nueva) va en la última columna.
- **Los 4 campos de percepción son numéricos**: van en `0.00`, no en `-` como el resto de lo vacío (error *"'-' no es un valor válido para 'decimal'"*). Los 4 de documento modificado, en cambio, van **vacíos** — la plantilla del SFS los compara contra cadena vacía, así que un `-` haría que arme nodos con basura.
- **El ticket se consume al consultarlo.** Si el daemon lo usa y el resumen queda en `08`, el SFS vuelve a consultarlo, recibe *"El ticket no existe"* y lo deja en `05` (bloqueado), con sus archivos atascados en `DATA` pese a estar bien emitido. Por eso, al bajar el CDR, el daemon cierra el resumen en la bandeja (`_cerrar_resumen_en_sfs()`).

## Requisitos

- Python 3.10+
- PostgreSQL accesible (la base de la aplicación)
- SFS v2.1 instalado y corriendo localmente
- [PM2](https://pm2.keymetrics.io/) (opcional, para gestionar el proceso)

## Instalación

**Para poner el facturador en la PC de un cliente**, no hace falta nada de este README: lo hace todo el instalador `FacturadorSetup.exe` —prerrequisitos, SFS, datos del contribuyente, certificado y procesos— y deja la máquina emitiendo. El procedimiento está en el manual en PDF; cómo está armado, en [`instalacion/README.md`](instalacion/README.md).

**Para trabajar sobre el código**, en cambio, hay que preparar el entorno a mano:

```bash
pip install -r requirements.txt
```

El daemon corre con el Python de la máquina, no con el que lleva adentro el instalador: PM2 lo arranca como `main.py` con `interpreter: "python"`. Por eso esas tres dependencias tienen que estar instaladas en el sistema, y por eso el instalador lee `requirements.txt` en su primer paso.

## Configuración

Crea un archivo `.env` en la raíz del proyecto (no se versiona) con estas variables:

```env
# Base de datos de la aplicación. Es la misma DATABASE_URL que usa el sistema:
# los parámetros de Prisma (?schema=...) se traducen solos.
DATABASE_URL=postgresql://usuario:clave@host:5432/postgres?schema=public

# Facturador SFS
SFS_BASE_URL=http://localhost:9000
SFS_DATA_DIR=C:\SFS_v2.1\sunat_archivos\sfs\DATA
SFS_RPTA_DIR=C:\SFS_v2.1\sunat_archivos\sfs\RPTA
SFS_BD_PATH=C:\SFS_v2.1\bd\BDFacturador.db

# Intervalo de polling en segundos
INTERVALO_GENERACION_SEG=60

# Barrido de respaldo de la carpeta RPTA, en segundos (además de los eventos
# en tiempo real de watchdog). Opcional, por defecto 30.
INTERVALO_BARRIDO_RPTA_SEG=30

# RUC del emisor (dejar vacío para leerlo de la BD)
EMISOR_RUC=

# Cuántas veces se reenvía un comprobante rechazado por SUNAT. Opcional, por defecto 3.
MAX_REINTENTOS_RECHAZO=3

# Credenciales SOL, para las dos consultas que el daemon le hace a SUNAT: el ticket
# de cada resumen diario y el CDR de un comprobante que quedó sin respuesta.
# SIN ESTO NO SE CIERRA NINGÚN RESUMEN: su CDR llega detrás de un ticket y no hay
# otra forma de traerlo (ver "Resumen diario de boletas").
SOL_USUARIO=
SOL_CLAVE=

# Minutos sin CDR antes de consultar a SUNAT. Opcional, por defecto 10.
CONSULTA_SUNAT_TRAS_MIN=10

# Corrección horaria entre el reloj de la BD y la hora local, en horas. Por defecto
# "auto": se mide en cada ciclo (ver "Fecha de emisión y zona horaria"). Solo poner
# un número si hiciera falta forzarlo.
DESFASE_BD_HORAS=auto
```

## Uso

`sfs.config.js` gestiona dos procesos: el **SFS** (la aplicación Java de SUNAT) y el **daemon**. Levantar los dos:

```bash
pm2 start sfs.config.js
```

Para que arranquen solos al encender la PC (Windows no tiene init system, así que `pm2 startup` no alcanza):

```bash
npm install -g pm2-windows-startup
pm2-startup install
pm2 save
```

La bandeja del SFS (`http://localhost:9000`) no hace falta para que el daemon funcione — se abre solo para mirar. Ejecución directa, sin PM2:

```bash
python main.py
```

Los logs se escriben en `facturador.log` y, si se usa PM2, también en `logs/out.log` / `logs/error.log`.

## Flujo del comprobante

```
Factura (enviado=false)
        ↓
ciclo_generacion() cada 60s
        ↓
Factura y notas ─┐                    ┌─ Boletas: se acumulan y salen
  una por una    │                    │  agrupadas en un resumen diario
                 ↓                    ↓
        Genera los archivos en DATA (ver tablas más arriba)
                 ↓
        El SFS relee DATA (sincronizar_bandeja_sfs)
                 ↓
        Envía al SFS local → XML firmado → SUNAT
                 ↓
    ┌────────────┴────────────┐
    ↓                         ↓
CDR directo            Ticket → getStatus
(factura, notas)       (resumen diario)
    └────────────┬────────────┘
                 ↓
        El CDR (ZIP) queda en RPTA
                 ↓
        Hilo CDR detecta el ZIP y lo procesa
                 ↓
Si ACEPTADO → enviado=true en la BD (una fila, o todas las boletas
              del resumen), ZIP a RPTA/procesados/ y se borra DATA
```

El SFS trabaja en dos pasadas: la primera registra el archivo en su bandeja y la segunda genera el XML, así que un comprobante nuevo suele necesitar **dos ciclos** para salir. Un resumen tarda algo más, porque su CDR llega detrás de un ticket.

## Limitaciones conocidas

**Un ítem sin IGV no se puede emitir.** El detalle se arma siempre como gravado al 18% (`tipAfeIGV=10`), sin mirar los datos. Si un ítem llega con IGV en 0 —una cortesía, un servicio exonerado— el XML declara "gravado al 18%" con importe 0 y SUNAT lo rechaza con el error `3111`. Resolverlo bien exige una columna de tipo de afectación en `FacturaItem`, que hoy no existe; el daemon no puede deducirlo del monto sin arriesgar una declaración incorrecta.

**El valor unitario pierde precisión.** Se redondea a 2 decimales y luego se escribe con 6, así que `cantidad x valor_unitario` puede diferir en céntimos del valor de venta declarado. SUNAT lo tolera en comprobantes chicos, pero el error se acumula con la cantidad de líneas.

**Sin comunicación de baja.** Anular un comprobante ya aceptado (`RA`) todavía no se emite desde acá.

**El resumen diario no maneja rechazo parcial de una línea.** Si SUNAT acepta el resumen pero observa una boleta puntual dentro de él, ese caso no se detecta automáticamente y necesita revisión manual (ver "Resumen diario de boletas").

**`facturador.log` no rota.** Con volumen alto conviene agregarle rotación.

## Pruebas contra el ambiente beta

El SFS apunta a producción o a homologación según qué línea `RUTA_SERV_CDP` esté descomentada en `sunat_archivos/sfs/VALI/constantes.properties` (hay que reiniciarlo para que la tome: `pm2 restart sfs`). Mientras la activa sea `e-beta.sunat.gob.pe`, los comprobantes llegan a SUNAT pero **no tienen validez fiscal**: es el ambiente donde conviene probar cualquier cambio al generador de archivos. Las credenciales SOL son las mismas en los dos ambientes; lo único que cambia es esa URL.

> **Mientras el SFS esté en beta, ningún comprobante real debe llegar a la cola.** El daemon lo daría por emitido (`enviado=true`) sobre un envío sin validez fiscal, y no hay nada en la base que después delate la diferencia. Conviene probar con una serie propia (`B999-*`, por ejemplo) y borrarla al terminar.

Escenarios que vale la pena cubrir antes de dar por bueno un cambio, porque cada uno ejercita un camino distinto del código:

- Factura y boleta simples
- Boleta a consumidor final (sin `ReceptorId`)
- Comprobante con varios ítems y valores unitarios de muchos decimales
- Nota de crédito sobre factura y sobre boleta
- Nota de crédito con motivo de anulación total (`01`) y de devolución parcial (`07`)
- Nota de débito, que es la única que se emite contra una factura
- Resumen diario: boletas con fecha anterior a hoy, para confirmar que se agrupan y no se envían una por una

### Antes de pasar a producción

1. Vaciar los comprobantes de prueba de `Factura` e `FacturaItem`
2. Volver a poner `Correlativo.numeracion` en `0`, o la primera factura real no arrancará en 1
3. Vaciar la tabla `DOCUMENTO` de la base del SFS y las carpetas `DATA` y `RPTA`
4. Borrar `resumenes.json`, o el correlativo de los resúmenes seguiría desde donde quedó en las pruebas
5. En la configuración del SFS: certificado digital real, usuario y clave SOL reales, y los datos del emisor completos — un nombre comercial vacío se emite como `-` y SUNAT lo observa con el código `4092`
6. Cambiar `RUTA_SERV_CDP` a producción y **reiniciar el SFS** (`pm2 restart sfs`); confirmar el cambio antes de seguir
7. Recién entonces emitir **un solo** comprobante para verificar antes de soltar el resto
