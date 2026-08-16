# Facturador

Daemon de Facturación Electrónica SUNAT (SFS v2.1).

Corre en segundo plano y emite comprobantes sin intervención: lee los pendientes de la base de datos de la aplicación, genera los archivos que necesita el SFS (Sistema de Facturación SUNAT), se los entrega —él firma el XML y lo envía— y procesa las respuestas (CDR) de SUNAT para cerrar cada comprobante.

Factura, nota de crédito y nota de débito salen de a una; las boletas salen agrupadas en un resumen diario. Trabaja con dos hilos:

- **Generador**: cada `INTERVALO_GENERACION_SEG` (60s) revisa si hay comprobantes por enviar.
- **CDR**: vigila `RPTA` y procesa el ZIP en cuanto SUNAT responde, con un barrido de respaldo cada `INTERVALO_BARRIDO_RPTA_SEG`.

**El SFS no vigila `DATA`**: solo la escanea al cargar su pantalla (`cargarArchivosContribuyente`, que cuelga de `CargarPantalla.htm`). Ni `GenerarComprobante.htm` ni `enviarXML.htm` lo hacen — esos operan sobre lo que ya está en su bandeja. Por eso el daemon llama a `sincronizar_bandeja_sfs()` al inicio de cada ciclo y antes de entregar documentos. Sin eso dependería de que alguien tuviera la bandeja abierta en el navegador: con la ventana cerrada los archivos se quedan en `DATA` y **no se emite nada**.

## Tipos de comprobante

Factura (`01`), nota de crédito (`07`) y nota de débito (`08`) individuales; boleta (`03`) dentro del resumen diario (`RC`). Para anular un comprobante ya aceptado se emite una nota de crédito.

Las notas exigen la referencia al documento que corrigen, y esos campos salen de `Factura`:

| Campo del SFS | Columna |
|---|---|
| `codMotivo` | `tipoNota` (catálogo 09 para NC, 10 para ND) |
| `desMotivo` | `motivoDocumentoAfectado` (si está vacío, la descripción del catálogo) |
| `tipDocAfectado` | `tipoDocumentoAfectado` |
| `numDocAfectado` | `numeracionDocumentoAfectado` |

Si a una nota le falta `tipoNota`, `tipoDocumentoAfectado` o `numeracionDocumentoAfectado`, **no se emite**: queda un WARNING y se reintenta cuando alguien complete el dato. El motivo no se deduce ni se rellena por defecto — una nota con el motivo equivocado es una declaración incorrecta ante SUNAT.

## Validaciones previas

Antes de escribir cualquier archivo, todo comprobante —y cada boleta candidata al resumen— pasa por `_validar_campos_obligatorios()`: numeración, fecha de emisión válida y total; los individuales además necesitan al menos un ítem. Si falta algo **no se genera nada**: WARNING con el detalle y reintento en el próximo ciclo, cuando se corrija el dato.

Eso incluye las ventas que la aplicación cobró pero nunca numeró. Antes se descartaban en el SQL de las consultas de pendientes y **desaparecían sin dejar una línea en el log**; ahora llegan a la validación, que las identifica por su `id` de fila ya que no tienen numeración. El aviso sale **una vez por corrida**, no en cada ciclo: el dato no se corrige solo y repetir la misma línea cada 60 segundos llenaría el log. Si sigue sin resolverse, reaparece en el próximo arranque.

No valida dígito verificador de RUC/DNI ni cuadre de totales. Tampoco mira el IGV de cada ítem: el detalle se arma siempre como gravado al 18% (`tipAfeIGV=10`), correcto para los servicios de estética que es lo que se factura acá, pero un ítem exonerado o de cortesía haría que SUNAT rechace con el error `3111`. Resolverlo exige una columna de tipo de afectación en `FacturaItem`, que la aplicación no tiene.

## Fecha de emisión y zona horaria

La aplicación guarda sus fechas con el reloj de su servidor de base de datos, que corre en **UTC**; SUNAT espera la hora local del emisor. Sin corregirlo, toda venta entre las 19:00 y la medianoche (Perú es UTC−5) queda con la fecha del día siguiente: a SUNAT se le declararía una fecha que todavía no llegó, y en el resumen diario la boleta caería en el día equivocado.

Por eso el daemon **mide** el desfase en vez de asumirlo: al inicio de cada ciclo compara el reloj de la BD con la hora local y aplica esa corrección (`detectar_desfase_bd()` → `fecha_local()`). Si cambian la zona horaria del servidor, se ajusta solo en el ciclo siguiente; si la medición falla, conserva la última corrección conocida en vez de arriesgar una fecha equivocada. Se puede forzar con `DESFASE_BD_HORAS`.

## Archivos que genera en DATA

No son los mismos para todos los tipos, y equivocarse hace que el SFS rechace el documento. La boleta no genera ninguno de estos: va en el resumen diario (`.RDI`/`.TRD`).

| Archivo | Factura `01` | Notas `07` / `08` |
|---|:---:|:---:|
| `.cab` — cabecera, 18 columnas | ✓ | — |
| `.NOT` — cabecera de nota, 21 columnas | — | ✓ |
| `.det` — detalle, **36 columnas** | ✓ | ✓ |
| `.tri` — tributos | ✓ | ✓ |
| `.ley` — leyenda | ✓ | ✓ |
| `.PAG` — forma de pago | ✓ | — |

Tres reglas que no están documentadas por SUNAT:

- **La cabecera de las notas va en `.NOT`, no en `.cab`.** El SFS identifica cada documento por la extensión de su cabecera; con la cabecera en `.cab` responde *"El archivo no existe: ...NOT"* aunque el contenido sea correcto.
- **La forma de pago solo va en facturas.** En boletas, SUNAT lee `cac:PaymentTerms` como detracción y devuelve el error `3128`. En notas ese nodo solo admite `Credito` o `Cuota*`, así que `Contado` da el error `3246`.
- **El detalle siempre lleva 36 columnas.** El mensaje de error del SFS para la nota de débito dice *"(30 columnas)"*, pero su código compara contra 36.

Los archivos se borran de `DATA` al final del ciclo en que el SFS cierra el documento (`IND_SITU` `03` o `04`). Los de comprobantes bloqueados o rechazados se conservan, porque sirven para diagnosticar.

## Estados y reintentos

El daemon se guía por el `IND_SITU` que el SFS lleva en su propia base:

| Estado | Significado | Qué hace el daemon |
|---|---|---|
| `03` / `04` | Aceptado (con o sin observaciones) | Cierra con `enviado=true` |
| `05` | Anulado | `BLOQUEADO`; **no** lo reenvía |
| `06` | Con errores | `BLOQUEADO`; no lo reenvía |
| `10` | Rechazado por SUNAT | Lo regenera y reenvía, hasta `MAX_REINTENTOS_RECHAZO` veces |

Ante un CDR de rechazo el motivo se guarda en `Factura.errors` (código, descripción y fecha) además del log, el ZIP se archiva en `RPTA/errores/` y el comprobante nunca pasa a `enviado=true`.

**Tope de reenvíos.** Un reenvío manda exactamente los mismos datos, así que si SUNAT rechazó por un dato mal armado el resultado no cambia. De ahí el tope (3 por defecto); al agotarlo el comprobante queda `BLOQUEADO` hasta que se corrija. El conteo va en `reintentos.json` (no se versiona) para que un reinicio de PM2 no reinicie el ciclo:

```json
{ "F001-000107": { "tipo": "01", "intentos": 3, "ultimo": "2026-08-14 15:33:05",
                   "motivo": "2800 - El dato ingresado en el tipo de documento no es valido" } }
```

El contador se borra solo con el CDR de aceptación. Para reintentar antes, elimina esa entrada del archivo.

### Recuperación tras un corte de conexión

Si el SFS alcanza a enviar y la respuesta nunca vuelve, nadie sabe si SUNAT lo registró; reenviar a ciegas arriesga un duplicado, que solo se deshace con una nota de crédito. Por eso, al inicio de cada ciclo, el daemon toma los comprobantes que el SFS marcó como enviados pero llevan más de `CONSULTA_SUNAT_TRAS_MIN` minutos sin CDR y consulta directo a SUNAT por SOAP (`billConsultService`):

- **Lo tiene**: descarga el CDR a `RPTA` y el hilo CDR lo procesa como si hubiera llegado normal.
- **No lo tiene**: lo saca de la bandeja del SFS para que el próximo ciclo lo regenere.
- **Cualquier otra respuesta** —código desconocido, falla de red, credenciales ausentes—: no toca nada. Ante la duda, nunca reenvía.

Requiere `SOL_USUARIO` y `SOL_CLAVE`; si faltan, el paso no corre y el resto sigue igual. El servicio de consulta **solo existe en producción**, pero consultar es de solo lectura y no depende de a qué ambiente apunte el SFS para enviar.

## Resumen diario de boletas

Ninguna boleta se envía individualmente. Todas salen agrupadas en un resumen diario (`RC`), que para el SFS es un tipo de documento más (mismos endpoints REST, mismo patrón de dos pasadas) pero que SUNAT procesa con un flujo de ticket en vez de respuesta inmediata.

Esto evita el problema de origen: una boleta enviada más de 5 días después de su emisión, SUNAT la rechaza para envío individual (`IND_SITU='06'`) y queda bloqueada para siempre. Agrupada en un resumen, ese límite no aplica.

En cada ciclo:

1. `obtener_boletas_para_resumen()` junta las boletas con `enviado=false` y fecha anterior a hoy —las de hoy esperan al resumen del día siguiente—, salvo las que ya estén en un resumen en curso.
2. `generar_resumen_diario()` les asigna una numeración propia del daemon, `RC-YYYYMMDD-NNN` (correlativo en `resumenes.json`, no se versiona), y escribe `.RDI` (una línea por boleta, 23 columnas) y `.TRD` (tributos por línea, 6 columnas). Si un número ya tiene su CDR en disco lo saltea: sin eso, un `resumenes.json` perdido devolvía el contador a 001 y el daemon tomaba el CDR anterior por la respuesta del resumen nuevo.
3. Se entrega al SFS igual que cualquier documento (`activar_procesamiento_sfs()`).
4. SUNAT responde un **ticket**, no el CDR. `recuperar_cdr_resumenes()` lo consulta por SOAP (`getStatus`) hasta que está listo. El ticket **se consume al consultarlo**: si el resumen quedara abierto en la bandeja, el SFS volvería a consultarlo, recibiría *"El ticket no existe"* y lo dejaría en `05` pese a estar bien emitido. Por eso el daemon lo cierra en la bandeja (`_cerrar_resumen_en_sfs()`) una vez procesado el CDR.
5. Como un resumen agrupa muchas filas de `Factura` en vez de ser una, el cierre hace `UPDATE ... WHERE "numeracionComprobante" = ANY(...)` con la lista guardada en `resumenes.json`.
6. Un resumen rechazado se reintenta con el mismo tope; agotado, queda `BLOQUEADO` y sus boletas no entran a uno nuevo hasta que se revise.

Tres reglas del formato que el `.RDI` no perdona:

- **El nombre de archivo va sin el `RC-` del id.** `validarNombreArchivo()` exige exactamente 4 tramos separados por guión (`RUC-TIPO-SERIE-NÚMERO`); con el prefijo son 5 y el SFS **descarta el archivo en silencio**, sin generar el XML ni dejar rastro. En todo lo demás (bandeja, API REST, CDR) el id sí lleva el `RC-`.
- **`tipDocResumen` (columna 3) es el tipo de comprobante (`03`), no el estado de la línea.** Confundirlos da el error `2241`. El estado (`1` = nueva) va en la última columna.
- **Los 4 campos de percepción son numéricos**: van en `0.00`, no en `-` como el resto de lo vacío (*"'-' no es un valor válido para 'decimal'"*). Los 4 de documento modificado, en cambio, van **vacíos**: la plantilla los compara contra cadena vacía y un `-` le haría armar nodos con basura.

SUNAT puede aceptar el resumen y aun así observar boletas puntuales: cada una viene en su propio `<cac:DocumentResponse>`, que el esquema declara repetible. El daemon los recorre todos, guarda la observación en la `Factura` que corresponde y lo avisa en el log.

## Requisitos

Python 3.10+, PostgreSQL accesible, SFS v2.1 corriendo localmente y [PM2](https://pm2.keymetrics.io/) (opcional, para gestionar el proceso).

## Instalación

**En la PC de un cliente** no hace falta nada de este README: lo hace todo `FacturadorSetup.exe` —prerrequisitos, SFS, datos del contribuyente, certificado y procesos—. El procedimiento está en el manual en PDF; cómo está armado, en [`instalacion/README.md`](instalacion/README.md).

**Para trabajar sobre el código**, en cambio:

```bash
pip install -r requirements.txt
```

El daemon corre con el Python de la máquina, no con el que lleva adentro el instalador: PM2 lo arranca como `main.py` con `interpreter: "python"`. Por eso esas tres dependencias van instaladas en el sistema, y por eso el instalador lee `requirements.txt` en su primer paso.

## Configuración

Un archivo `.env` en la raíz (no se versiona). Solo las cuatro primeras son obligatorias; el resto tiene valor por defecto.

```env
# Base de la aplicación. La misma DATABASE_URL que usa el sistema: los
# parámetros de Prisma (?schema=...) se traducen solos.
DATABASE_URL=postgresql://usuario:clave@host:5432/postgres?schema=public

SFS_DATA_DIR=C:\SFS_v-2.1\sunat_archivos\sfs\DATA
SFS_RPTA_DIR=C:\SFS_v-2.1\sunat_archivos\sfs\RPTA
SFS_BD_PATH=C:\SFS_v-2.1\bd\BDFacturador.db

# Credenciales SOL. SIN ESTO NO SE CIERRA NINGÚN RESUMEN: su CDR llega detrás
# de un ticket y no hay otra forma de traerlo.
SOL_USUARIO=
SOL_CLAVE=

SFS_BASE_URL=http://localhost:9000
EMISOR_RUC=                      # vacío = se lee de la BD
INTERVALO_GENERACION_SEG=60
INTERVALO_BARRIDO_RPTA_SEG=30    # barrido de respaldo de RPTA
MAX_REINTENTOS_RECHAZO=3         # reenvíos de un comprobante rechazado
CONSULTA_SUNAT_TRAS_MIN=10       # minutos sin CDR antes de consultar a SUNAT
DESFASE_BD_HORAS=auto            # "auto" lo mide en cada ciclo
LOG_MAX_MB=5                     # rotación de facturador.log
LOG_ARCHIVOS=5
```

Las rutas del SFS llevan **guión** en `SFS_v-2.1`, que es como lo nombra SUNAT. Si la ruta no existe, el daemon escribe en una carpeta local que el SFS nunca mira y **no se emite nada, sin error**.

## Uso

`sfs.config.js` gestiona dos procesos: el **SFS** (la aplicación Java de SUNAT) y el **daemon**. Lo genera la opción 2 del instalador con las rutas de cada PC; no se versiona.

```bash
pm2 start sfs.config.js
```

Al SFS se lo arranca con topes de memoria (`-Xms64m -Xmx512m -XX:MaxMetaspaceSize=256m -XX:+UseSerialGC`). Sin ellos el JVM se autoasigna hasta un cuarto de la RAM del equipo y usa el recolector paralelo, con un hilo por núcleo: en una PC de 8 GB eso lo dejaba sin memoria para arrancar y **se moría en silencio**, sin escribir nada en su log de errores — y con el SFS caído el daemon no emite. Con los topes pasa de 850 MB a 270 MB. No reservan memoria: solo impiden que crezca sin control.

Para que arranquen solos al encender la PC (Windows no tiene init system, así que `pm2 startup` no alcanza):

```bash
npm install -g pm2-windows-startup
pm2-startup install
pm2 save
```

La bandeja del SFS (`http://localhost:9000`) no hace falta para que el daemon funcione, se abre solo para mirar. Sin PM2: `python main.py`.

Los logs van a `facturador.log` y, con PM2, también a `logs/out.log` y `logs/error.log`. `facturador.log` rota a los 5 MB y conserva 5 archivos —unos 25 MB—, suficiente para investigar un rechazo semanas después sin crecer sin límite en una PC que va a estar años emitiendo.

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

El SFS trabaja en dos pasadas —la primera registra el archivo en su bandeja, la segunda genera el XML—, así que un comprobante nuevo suele necesitar **dos ciclos** para salir. Un resumen tarda algo más, porque su CDR llega detrás de un ticket.

## Lo que el daemon no decide solo

Dos situaciones en las que se detiene y deja el caso a la vista en vez de resolverlo por su cuenta:

**Una boleta observada dentro de un resumen aceptado queda emitida.** El daemon detecta la observación y guarda el motivo en `Factura.errors`, pero no corrige ni reemite: no puede saber si el reparo amerita rehacer el comprobante.

**Una boleta puede quedar retenida.** Si un resumen desaparece sin rastro —ni en la bandeja del SFS ni en `DATA`— el daemon no sabe si llegó a SUNAT, y retiene sus boletas antes que arriesgarse a declararlas dos veces. Lo avisa en el log con la instrucción para destrabarlas: borrar ese resumen de `resumenes.json`. Es deliberado —una boleta retenida es reversible, una declarada dos veces no— pero requiere que alguien lo mire.

## Pruebas contra el ambiente beta

El SFS apunta a producción o a homologación según qué línea `RUTA_SERV_CDP` esté descomentada en `sunat_archivos/sfs/VALI/constantes.properties` (hay que reiniciarlo para que la tome: `pm2 restart sfs`). Con `e-beta.sunat.gob.pe` activa, los comprobantes llegan a SUNAT pero **no tienen validez fiscal**. Las credenciales SOL son las mismas en ambos ambientes; lo único que cambia es esa URL.

> **Mientras el SFS esté en beta, ningún comprobante real debe llegar a la cola.** El daemon lo daría por emitido (`enviado=true`) sobre un envío sin validez fiscal, y nada en la base delata después la diferencia. Conviene probar con una serie propia (`B999-*`) y borrarla al terminar.

Escenarios que vale la pena cubrir antes de dar por bueno un cambio, porque cada uno ejercita un camino distinto:

- Factura y boleta simples
- Boleta a consumidor final (sin `ReceptorId`)
- Comprobante con varios ítems y valores unitarios de muchos decimales
- Nota de crédito sobre factura y sobre boleta
- Nota de crédito con motivo de anulación total (`01`) y de devolución parcial (`07`)
- Nota de débito, que solo se emite contra una factura
- Resumen diario: boletas con fecha anterior a hoy, para confirmar que se agrupan

### Antes de pasar a producción

1. Vaciar los comprobantes de prueba de `Factura` e `FacturaItem`
2. Volver a poner `Correlativo.numeracion` en `0`, o la primera factura real no arranca en 1
3. Vaciar la tabla `DOCUMENTO` de la base del SFS y las carpetas `DATA` y `RPTA`
4. Borrar `resumenes.json`, o el correlativo de los resúmenes sigue desde las pruebas
5. En el SFS: certificado real, usuario y clave SOL reales, y datos del emisor completos — un nombre comercial vacío se emite como `-` y SUNAT lo observa con el código `4092`
6. Cambiar `RUTA_SERV_CDP` a producción y **reiniciar el SFS** (`pm2 restart sfs`); confirmar el cambio antes de seguir
7. Recién entonces emitir **un solo** comprobante para verificar antes de soltar el resto
