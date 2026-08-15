# Facturador

Daemon de Facturación Electrónica SUNAT (SFS v2.1).

Corre en segundo plano y emite comprobantes electrónicos a SUNAT sin intervención: lee los pendientes de una base de datos SQL Server, genera los archivos que necesita el SFS (Sistema de Facturación SUNAT), los entrega al facturador local —que firma el XML y lo envía— y procesa las respuestas (CDR) de SUNAT para cerrar cada comprobante.

Emite facturas, boletas, notas de crédito y notas de débito.

Corre dos hilos en paralelo:
- **Hilo Generador**: cada `INTERVALO_GENERACION_SEG` segundos (60s por defecto) revisa si hay comprobantes por enviar.
- **Hilo CDR**: monitorea en tiempo real la carpeta `RPTA` y procesa el ZIP en cuanto SUNAT responde, con un barrido de respaldo cada `INTERVALO_BARRIDO_RPTA_SEG` segundos.

## Tipos de comprobante

Se emiten factura (`01`), boleta (`03`), nota de crédito (`07`) y nota de débito (`08`). El resumen diario (`RC`) y la comunicación de baja (`RA`) todavía no.

Las notas exigen la referencia al documento que corrigen, y esos campos salen de `Comprobantes`:

| Campo del SFS | Columna |
|---|---|
| `codMotivo` | `tipo_nota` (catálogo 09 para NC, 10 para ND) |
| `desMotivo` | `motivo_documento_afectado` (si está vacío se usa la descripción del catálogo) |
| `tipDocAfectado` | `tipo_documento_afectado` |
| `numDocAfectado` | `numeracion_documento_afectado` |

Si a una nota le falta `tipo_nota`, `tipo_documento_afectado` o `numeracion_documento_afectado`, **no se emite**: se registra un WARNING y se reintenta cuando alguien complete el dato. El código de motivo no se deduce ni se rellena por defecto, porque una nota con el motivo equivocado es una declaración incorrecta ante SUNAT.

## Archivos que genera en DATA

No son los mismos para todos los tipos, y equivocarse hace que el SFS rechace el documento:

| Archivo | Factura `01` | Boleta `03` | Notas `07` / `08` |
|---|:---:|:---:|:---:|
| `.cab` — cabecera, 18 columnas | ✓ | ✓ | — |
| `.NOT` — cabecera de nota, 21 columnas | — | — | ✓ |
| `.det` — detalle, **36 columnas** | ✓ | ✓ | ✓ |
| `.tri` — tributos | ✓ | ✓ | ✓ |
| `.ley` — leyenda | ✓ | ✓ | ✓ |
| `.PAG` — forma de pago | ✓ | — | — |

Tres reglas que no están documentadas por SUNAT y que se descubrieron probando contra el ambiente beta:

- **La cabecera de las notas va en `.NOT`, no en `.cab`.** El SFS identifica cada documento por la extensión de su archivo de cabecera. Con la cabecera en `.cab` responde *"El archivo no existe: ...NOT"* aunque el contenido sea correcto.
- **La forma de pago solo va en facturas.** En boletas, el validador de SUNAT lee `cac:PaymentTerms` como información de detracción y devuelve el error `3128`. En notas, ese nodo solo admite `Credito` o `Cuota*`, así que `Contado` devuelve el error `3246`.
- **El detalle siempre lleva 36 columnas.** El mensaje de error del SFS para la nota de débito dice *"(30 columnas)"*, pero su código compara contra 36. Seguir ese mensaje hace que rechace el archivo.

Los archivos se borran de `DATA` al final del ciclo en que el SFS cierra el documento (`IND_SITU` `03` o `04`). Los de comprobantes bloqueados o rechazados se conservan, porque sirven para diagnosticar.

## Estados y reintentos

El daemon se guía por el `IND_SITU` que el SFS lleva en su propia base:

| Estado | Significado | Qué hace el daemon |
|---|---|---|
| `03` / `04` | Aceptado (con o sin observaciones) | Cierra con `enviado=1` |
| `05` | Anulado | Lo reporta como `BLOQUEADO`; **no** lo reenvía |
| `06` | Con errores (p. ej. boleta de más de 5 días, que exige resumen diario) | Lo reporta como `BLOQUEADO`; no lo reenvía |
| `10` | Rechazado por SUNAT | Lo regenera y lo reenvía, hasta `MAX_REINTENTOS_RECHAZO` veces |

Cuando llega un CDR de rechazo, el motivo se guarda en `Comprobantes.errors` (código y descripción de SUNAT, con fecha) además de quedar en el log, y el ZIP se archiva en `RPTA/errores/`. El comprobante nunca pasa a `enviado=1`.

### Tope de reenvíos

Un reenvío manda **exactamente los mismos datos**, así que si SUNAT rechazó por un dato mal armado el resultado no cambia. Por eso cada comprobante tiene un tope de reenvíos (3 por defecto); al agotarlo deja de reintentarse y pasa a reportarse como `BLOQUEADO` hasta que se corrija el dato.

El conteo se guarda en `reintentos.json` (no se versiona) para que un reinicio de PM2 no reinicie el ciclo:

```json
{ "F001-000107": { "tipo": "01", "intentos": 3, "ultimo": "2026-08-14 15:33:05",
                   "motivo": "2800 - El dato ingresado en el tipo de documento no es valido" } }
```

El contador se borra solo cuando llega el CDR de aceptación. Si corriges el dato y quieres reintentar antes de eso, elimina esa entrada del archivo.

## Requisitos

- Python 3.10+
- SQL Server accesible (driver ODBC instalado)
- SFS v2.1 instalado y corriendo localmente
- [PM2](https://pm2.keymetrics.io/) (opcional, para gestionar el proceso)

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Crea un archivo `.env` en la raíz del proyecto (no se versiona) con estas variables:

```env
# SQL Server
DB_DRIVER={SQL Server}
DB_SERVER=.\SQLEXPRESS
DB_DATABASE=AUXILIAR
DB_TRUSTED=yes

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
```

## Uso

Ejecución directa:

```bash
python main.py
```

Con PM2 (usando `sfs.config.js`):

```bash
pm2 start sfs.config.js
```

Los logs se escriben en `facturador.log` y, si se usa PM2, también en `logs/out.log` / `logs/error.log`.

## Flujo del comprobante

```
SQL Server (enviado=0)
        ↓
ciclo_generacion() cada 60s
        ↓
Genera los archivos del tipo en DATA (ver tabla más arriba)
        ↓
Envía al SFS local → XML firmado → SUNAT
        ↓
SUNAT responde con CDR (ZIP) en RPTA
        ↓
Hilo CDR detecta el ZIP y lo procesa
        ↓
Si ACEPTADO → enviado=1 en SQL Server, ZIP movido a RPTA/procesados/
                y se borran los archivos de DATA
```

El SFS trabaja en dos pasadas: la primera registra el archivo en su bandeja y la segunda genera el XML, así que un comprobante nuevo suele necesitar **dos ciclos** para salir.

## Limitaciones conocidas

**Un ítem sin IGV no se puede emitir.** El detalle se arma siempre como gravado al 18% (`tipAfeIGV=10`), sin mirar los datos. Si un ítem llega con IGV en 0 —una cortesía, un servicio exonerado— el XML declara "gravado al 18%" con importe 0 y SUNAT lo rechaza con el error `3111`. Resolverlo bien exige una columna de tipo de afectación en `Items`, que hoy no existe; el daemon no puede deducirlo del monto sin arriesgar una declaración incorrecta.

**El valor unitario pierde precisión.** Se redondea a 2 decimales y luego se escribe con 6, así que `cantidad x valor_unitario` puede diferir en céntimos del valor de venta declarado. SUNAT lo tolera en comprobantes chicos, pero el error se acumula con la cantidad de líneas.

**Sin resumen diario ni comunicación de baja.** Una boleta que pasa el plazo de envío queda en `IND_SITU='06'` y solo puede regularizarse con un resumen diario (`RC`), que todavía no se emite desde acá.

**`facturador.log` no rota.** Con volumen alto conviene agregarle rotación.

## Pruebas contra el ambiente beta

El SFS apunta a producción o a homologación según qué línea `RUTA_SERV_CDP` esté descomentada en `sunat_archivos/sfs/VALI/constantes.properties`. Mientras la activa sea `e-beta.sunat.gob.pe`, los comprobantes llegan a SUNAT pero **no tienen validez fiscal**: es el ambiente donde conviene probar cualquier cambio al generador de archivos.

Escenarios que vale la pena cubrir antes de dar por bueno un cambio, porque cada uno ejercita un camino distinto del código:

- Factura y boleta simples
- Boleta a consumidor final (sin `ReceptorId`)
- Comprobante con varios ítems y valores unitarios de muchos decimales
- Nota de crédito sobre factura y sobre boleta
- Nota de crédito con motivo de anulación total (`01`) y de devolución parcial (`07`)
- Nota de débito, que es la única que se emite contra una factura

### Antes de pasar a producción

1. Vaciar los comprobantes de prueba de `Comprobantes` e `Items`
2. Volver a poner `Correlativos.numeracion` en `000000`, o la primera factura real no arrancará en 1
3. Vaciar la tabla `DOCUMENTO` de la base del SFS y las carpetas `DATA` y `RPTA`
4. En la configuración del SFS: certificado digital real, usuario y clave SOL reales, y los datos del emisor completos — un nombre comercial vacío se emite como `-` y SUNAT lo observa con el código `4092`
5. Recién entonces, cambiar `RUTA_SERV_CDP` a producción y emitir **un solo** comprobante para confirmar antes de soltar el resto
