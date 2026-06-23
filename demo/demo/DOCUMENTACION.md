# Robot SUNAT – Documentación del Sistema
**Versión 1.0**

---

## ¿Qué hace este programa?

Este programa automatiza el envío de comprobantes electrónicos (facturas y boletas) a SUNAT. Lee los comprobantes registrados en la base de datos SQL Server, los convierte al formato que exige el **Sistema Facturador SUNAT (SFS)**, los envía a SUNAT y registra las respuestas de aceptación.

---

## Componentes del sistema

```
┌─────────────────────┐       ┌──────────────────────────┐       ┌─────────┐
│  Base de datos      │──────▶│  Este programa           │──────▶│  SFS    │──▶ SUNAT
│  SQL Server         │       │  (app_consola.py +       │       │  v2.1   │
│  DB: AUXILIAR       │       │   generador_sfs.py)      │◀──────│  :9000  │◀── CDR
└─────────────────────┘       └──────────────────────────┘       └─────────┘
```

### Componentes externos requeridos

| Componente | Ubicación | Descripción |
|---|---|---|
| **SQL Server AUXILIAR** | `.\SQLEXPRESS` | Base de datos principal con los comprobantes del negocio |
| **SFS v2.1** | `C:\SFS_v-2.1\facturadorApp-2.1.jar` | Aplicación Java de SUNAT que firma y envía XMLs |
| **Carpeta DATA** | `C:\SFS_v2.1\sunat_archivos\sfs\DATA` | El programa escribe aquí los archivos pipe que el SFS lee |
| **Carpeta RPTA** | `C:\SFS_v2.1\sunat_archivos\sfs\RPTA` | El SFS escribe aquí las respuestas (CDRs) de SUNAT |
| **SFS BD (SQLite)** | `C:\SFS_v-2.1\bd\BDFacturador.db` | Base de datos interna del SFS (tabla DOCUMENTO) |

---

## Archivos del programa

| Archivo | Función |
|---|---|
| `app_consola.py` | Menú de consola y procesamiento de respuestas CDR |
| `generador_sfs.py` | Generación de archivos DATA y comunicación con el SFS |

---

## Cómo ejecutar el programa

```
python app_consola.py
```

Aparece el siguiente menú:

```
========================================
   ROBOT SUNAT - MODO CONSOLA V1.0
========================================
1. Leer comprobantes
2. Leer y descargar PDFs
3. Generar archivos SFS pendientes
4. Procesar CDRs de SUNAT
5. Sincronizar (generar + procesar)    ← La opción más importante
6. Salir
========================================
```

---

## Opción 5: Sincronizar (flujo completo)

Esta es la opción principal. Hace todo el ciclo de envío y recepción en un solo paso.

### Diagrama de flujo

```
┌─────────────────────────────────────────────────────────┐
│ PASO 1: Procesar CDRs existentes                        │
│   Lee ZIP/XML en carpeta RPTA                           │
│   → Si son facturas: marca como Aceptado en SQL Server  │
│   → Si son RCs (boletas): registra confirmación         │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│ PASO 2: Generar archivos para comprobantes nuevos       │
│   Lee SQL Server: comprobantes con enviado = 0          │
│   → Por cada comprobante:                               │
│     - Genera archivos .cab .det .tri .ley .PAG en DATA  │
│     - Marca enviado = 1 en SQL Server                   │
│   → Activa el SFS: GenerarComprobante + enviarXML       │
│   → Detecta docs en '01' sin CDR y los activa también  │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│ PASO 3: Resumen Diario de Boletas (RC)                  │
│   Boletas > 5 días no se envían individualmente         │
│   → SFS las marca como estado '06' (más de 5 días)      │
│   → El programa agrupa las boletas '06' por fecha       │
│   → Genera un RC por cada fecha distinta                │
│   → Envía los RCs a SUNAT (respuesta asíncrona)         │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│ PASO 4: Esperar CDRs (90 segundos máximo)               │
│   Monitorea la carpeta RPTA cada 5 segundos             │
│   → Si aparecen CDRs: los procesa (PASO 1 de nuevo)     │
│   → Si no: termina con aviso                            │
└─────────────────────────────────────────────────────────┘
```

---

## Tipos de comprobantes

| Código | Tipo | Cómo se envía a SUNAT |
|---|---|---|
| `01` | Factura | Inmediato (síncrono) — CDR llega en segundos |
| `03` | Boleta de venta | Si tiene < 5 días: inmediato. Si tiene > 5 días: via RC (asíncrono) |
| `07` | Nota de crédito | Inmediato |
| `08` | Nota de débito | Inmediato |
| `RC` | Resumen Diario de Boletas | Asíncrono — SUNAT devuelve un ticket, la respuesta llega en minutos |
| `50`, `51`, `52` | Percepción, Retención, etc. | **No soportados por el SFS** — se ignoran |

---

## Archivos que genera el programa (formato pipe)

Por cada factura o boleta, el programa crea 5 archivos en la carpeta DATA. El nombre sigue el patrón: `{RUC}-{TIPO}-{SERIE}-{CORRELATIVO}.ext`

**Ejemplo:** `20480072872-01-F001-000053.cab`

| Extensión | Contenido |
|---|---|
| `.cab` | Cabecera: fecha, receptor, moneda, totales |
| `.det` | Detalle de ítems: descripción, cantidad, precio, IGV |
| `.tri` | Tributos: tipo de impuesto, base imponible, IGV |
| `.ley` | Monto en letras (ej: "SON CIEN CON 00/100 SOLES") |
| `.PAG` | Forma de pago (Contado / Crédito) |

Para el Resumen Diario de Boletas (RC), genera 4 archivos adicionales:

| Extensión | Contenido |
|---|---|
| `.cab` | Encabezado del RC: número, fecha de referencia, fecha de envío |
| `.RDI` | Líneas de boletas incluidas con sus importes |
| `.TRD` | Tributos por línea |
| `.DET` | Detalle de cada boleta |

---

## Tablas de la base de datos SQL Server

### Tabla `Comprobantes` (datos del negocio)

| Columna | Uso |
|---|---|
| `id` | Identificador interno |
| `tipo_comprobante` | `01`=Factura, `03`=Boleta, etc. |
| `numeracion_comprobante` | Serie-número (ej: `F001-000053`) |
| `fecha_emision` | Fecha del comprobante |
| `enviado` | `0` = pendiente de enviar, `1` = ya procesado |
| `ReceptorId` | Referencia a la tabla `Receptores` |

### Tabla `Emisores`

Contiene el RUC y razón social del emisor (empresa que emite los comprobantes).

### Tabla `Receptores`

Contiene datos del cliente: tipo de documento, número, razón social.

### Tabla `Items`

Contiene el detalle de productos/servicios de cada comprobante.

---

## Tabla DOCUMENTO del SFS (SQLite)

Esta es la bandeja de entrada del SFS. El programa escribe aquí los comprobantes que el SFS debe procesar.

| Columna | Descripción |
|---|---|
| `NUM_RUC` | RUC del emisor |
| `TIP_DOCU` | Tipo: `01`, `03`, `RC`, etc. |
| `NUM_DOCU` | Número de comprobante |
| `NOM_ARCH` | Nombre base de los DATA files |
| `IND_SITU` | **Estado actual** (ver tabla abajo) |
| `TIP_ARCH` | Formato del archivo — debe ser `TXT` para pipe |
| `NUM_TICKET` | Ticket de SUNAT (solo para RCs asincrónicos) |
| `DES_OBSE` | Observaciones / descripción del estado |

### Estados (IND_SITU) del SFS

| Estado | Significado |
|---|---|
| `01` | Por generar XML — el SFS lo cargó pero aún no generó el XML |
| `02` | XML generado — firmado y listo para enviar |
| `03` | ✅ Aceptado por SUNAT |
| `05` | ❌ Rechazado por SUNAT — se reintentará en el próximo ciclo |
| `06` | Boleta con más de 5 días — debe ir en un RC |
| `08` | Ticket pendiente — RC enviado, esperando respuesta asíncrona |
| `10` | ❌ Error interno del SFS |

---

## Ciclo de vida de una Factura

```
SQL Server                SFS Bandeja              SUNAT
enviado=0   ──────────▶  Estado '01'
                         (DATA files en disco)
                              │
                              ▼ GenerarComprobante
                         Estado '02'
                         (XML firmado listo)
                              │
                              ▼ enviarXML
                         Estado '03'  ──────────▶  Aceptado
                                      ◀──────────  CDR ZIP en RPTA
SQL Server
enviado=1   ◀──────────  CDR procesado
```

## Ciclo de vida de una Boleta (> 5 días)

```
SQL Server                SFS Bandeja              SUNAT
enviado=0   ──────────▶  Estado '01'
                              │
                              ▼ GenerarComprobante
                         Estado '06'
                         (más de 5 días)
                              │
                              ▼ El programa agrupa boletas '06'
                              │   y genera RC (Resumen Diario)
                         RC Estado '01'
                              │
                              ▼ GenerarComprobante + enviarXML
                         RC Estado '08' ─────────▶ SUNAT procesa
                         (ticket pendiente)        (tarda minutos)
                              │
                              ▼ ActualizarPantalla (polling)
                         RC Estado '03' ◀───────── CDR del RC en RPTA
                         Boleta Estado '03'
```

---

## Qué hacer si quedan docs en estado '01'

Puede ocurrir que al terminar la opción 5 queden 1 o 2 documentos en estado "Por generar XML" ('01') en la bandeja del SFS. Esto sucede porque el SFS tiene tareas en background que a veces no alcanzan a procesar todos los documentos antes del timeout de 90 segundos.

**Solución:** Ejecutar la opción 5 una segunda vez. El programa detecta automáticamente los documentos en '01' que no tienen respuesta CDR y los activa.

---

## Qué hacer si quedan RCs en ticket pendiente ('08')

Los RCs son asincrónicos. SUNAT puede tardar entre 30 segundos y varios minutos en responder.

**Solución:** Ejecutar la opción 5 nuevamente. Al inicio procesa los CDRs que hayan llegado a la carpeta RPTA y resuelve los tickets pendientes.

---

## Estructura de carpetas

```
C:\
├── SFS_v-2.1\                    ← Aplicación SFS (Java)
│   ├── facturadorApp-2.1.jar     ← El SFS corre en el puerto 9000
│   ├── bd\
│   │   └── BDFacturador.db       ← Base de datos SQLite del SFS
│   └── logs\                     ← Logs del SFS
│
└── SFS_v2.1\                     ← Datos del SFS
    └── sunat_archivos\sfs\
        ├── DATA\                 ← Archivos .cab/.det/.tri/.ley/.PAG que genera el programa
        ├── RPTA\                 ← CDRs de respuesta que pone el SFS
        │   └── procesados\       ← CDRs ya procesados (histórico)
        ├── PARSE\                ← XMLs firmados generados por el SFS
        └── FIRMA\                ← Archivos de firma digital
```

---

## Glosario

| Término | Significado |
|---|---|
| **SFS** | Sistema de Facturación SUNAT — aplicación Java que firma y envía XMLs a SUNAT |
| **CDR** | Constancia de Recepción — respuesta ZIP de SUNAT que confirma si aceptó o rechazó el comprobante |
| **RC** | Resumen Diario de Boletas — documento que agrupa boletas con más de 5 días para enviarlas juntas a SUNAT |
| **Ticket** | Número que devuelve SUNAT al recibir un RC asincrónico; se usa para consultar el resultado |
| **DATA files** | Archivos de texto con separador `\|` que el SFS lee para generar el XML UBL de SUNAT |
| **Pipe format** | Formato de los DATA files: campos separados por `\|` (pleca) |
| **TIP_ARCH** | Campo crítico del SFS — debe ser `TXT` para que el SFS procese correctamente los DATA files |
| **enviado** | Columna de SQL Server: `0`=pendiente, `1`=ya procesado por el programa |
| **IGV** | Impuesto General a las Ventas (18%) |
