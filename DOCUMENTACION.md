# Documentación — main.py
## Daemon de Facturación Electrónica SUNAT (SFS v2.1)

---

## ¿Qué hace este programa?

Es un programa que corre en segundo plano (daemon) y se encarga de enviar facturas y boletas a SUNAT automáticamente. Lee los comprobantes pendientes de una base de datos SQL Server, genera los archivos que necesita el SFS (Sistema de Facturación SUNAT), los envía al facturador local y procesa las respuestas de SUNAT.

Corre dos tareas en paralelo:
- **Hilo Generador**: cada 60 segundos revisa si hay comprobantes por enviar
- **Hilo CDR**: espera en tiempo real cuando SUNAT responde con un archivo ZIP

---

## Estructura del archivo

```
main.py
├── Importaciones
├── Configuración (variables de entorno)
├── Logging (registro de eventos)
├── Utilidades generales
├── Funciones de base de datos SQL Server
├── Generador de archivos SFS
├── API REST del SFS local
├── SFS BD SQLite — gestión de estados
├── Parser CDR — respuestas SUNAT
├── Flujo completo de generación
├── Hilo 1 — Generador
├── Hilo 2 — CDR
└── Main — punto de entrada
```

---

## Línea por línea

### Líneas 1–9 — Docstring inicial

```python
"""
main.py — Daemon de Facturación Electrónica SUNAT (SFS v2.1)
...
"""
```
Descripción del programa. Explica que tiene dos hilos: Generador (polling SQL Server) y CDR (monitorea carpeta RPTA).

---

### Importaciones

```python
import json        # leer/escribir JSON (respuestas de la API del SFS)
import logging     # registro de eventos en consola y archivo
import os          # manejo de rutas y archivos del sistema
import re          # expresiones regulares (limpiar nombres, extraer numeraciones)
import sqlite3     # conexión a la BD del SFS (BDFacturador.db)
import sys         # acceso a stdout para los logs
import threading   # crear los dos hilos paralelos
import time        # pausas entre ciclos y reintentos
import urllib.error, urllib.request  # llamadas HTTP a la API del SFS
import xml.etree.ElementTree as ET   # parsear XML de los CDRs de SUNAT
import zipfile     # abrir los ZIPs que envía SUNAT con el CDR
from datetime import datetime        # manejo de fechas
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP  # redondeo exacto de montos

import pyodbc            # conexión a SQL Server
from dotenv import load_dotenv       # leer el archivo .env con la configuración
from watchdog.observers import Observer          # monitorear carpeta RPTA
from watchdog.events import FileSystemEventHandler  # detectar cuando llega un ZIP
```

---

### Línea 38 — Directorio base

```python
_BASE = os.path.dirname(os.path.abspath(__file__))
```
Obtiene la ruta de la carpeta donde está `main.py`. Sirve como punto de referencia para encontrar el archivo `.env` y el log `facturador.log`.

---

### Línea 39 — Cargar variables de entorno

```python
load_dotenv(os.path.join(_BASE, ".env"))
```
Lee el archivo `.env` que está en la misma carpeta que `main.py`. A partir de aquí, `os.getenv("NOMBRE")` devuelve los valores definidos en ese archivo.

---

### Línea 42 — Intervalo de polling

```python
INTERVALO_GENERACION_SEG = int(os.getenv("INTERVALO_GENERACION_SEG", "60"))
```
Cuántos segundos espera entre cada ciclo de revisión de comprobantes. El valor por defecto es 60 segundos. Se puede cambiar en `.env`.

---

### Líneas 45–51 — Configuración SQL Server

```python
DB_CONFIG = {
    "driver":             os.getenv("DB_DRIVER",   "{SQL Server}"),
    "server":             os.getenv("DB_SERVER",   r".\SQLEXPRESS"),
    "database":           os.getenv("DB_DATABASE", "AUXILIAR"),
    "trusted_connection": os.getenv("DB_TRUSTED",  "yes"),
    "timeout":            30,
}
```
Parámetros para conectarse a SQL Server. Si no se definen en `.env`, usa los valores por defecto mostrados. `trusted_connection=yes` significa que usa autenticación de Windows (no usuario/contraseña).

---

### Líneas 54–55 — Rutas DATA y RPTA del SFS

```python
SFS_DATA_DIR = p if os.path.exists(p := os.getenv("SFS_DATA_DIR", r"C:\SFS_v2.1\...")) else ...
SFS_RPTA_DIR = p if os.path.exists(p := os.getenv("SFS_RPTA_DIR", r"C:\SFS_v2.1\...")) else ...
```
Rutas a las carpetas del SFS:
- **DATA**: donde se depositan los archivos `.cab`, `.det`, `.tri`, `.ley`, `.PAG` que genera el daemon
- **RPTA**: donde el SFS deposita los ZIPs de respuesta de SUNAT (CDRs)

El operador `:=` (walrus) intenta usar el valor del `.env`. Si la carpeta no existe, usa una ruta alternativa dentro de `_BASE`.

---

### Líneas 57–58 — BD del SFS y URL base

```python
SFS_BD_PATH  = os.getenv("SFS_BD_PATH",  r"C:\SFS_v2.1\bd\BDFacturador.db")
SFS_BASE_URL = os.getenv("SFS_BASE_URL", "http://localhost:9000")
```
- `SFS_BD_PATH`: ruta al archivo SQLite interno del SFS donde guarda el estado de cada comprobante
- `SFS_BASE_URL`: dirección de la API REST del SFS local

---

### Líneas 60–61 — Subcarpetas de RPTA

```python
DIR_PROCESADOS = os.path.join(SFS_RPTA_DIR, "procesados")
DIR_ERRORES    = os.path.join(SFS_RPTA_DIR, "errores")
```
Carpetas dentro de RPTA:
- `procesados/`: ZIPs procesados correctamente (CDR aceptado)
- `errores/`: ZIPs corruptos o con problemas

---

### Línea 64 — RUC override

```python
EMISOR_RUC_OVERRIDE = os.getenv("EMISOR_RUC", "").strip()
```
Permite forzar un RUC específico desde el `.env` en lugar de leerlo de la tabla `Emisores` de SQL Server. Útil cuando el RUC en la BD no coincide con el configurado en el SFS.

---

### Líneas 67–69 — Constantes de estado

```python
_ESTADOS_ERROR = ("05", "10")
_TIPOS_SFS = {"01", "03", "07", "08", "RC", "RA"}
```
- `_ESTADOS_ERROR`: códigos de `IND_SITU` en la BD del SFS que representan error o rechazo — estos comprobantes se reintentan
- `_TIPOS_SFS`: tipos de comprobante que el SFS puede procesar (01=Factura, 03=Boleta, 07=Nota Crédito, 08=Nota Débito, RC=Resumen, RA=Anulación)

---

### Líneas 75–87 — Configuración de logs

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),              # → PM2 out.log
        logging.FileHandler("facturador.log", encoding="utf-8"),  # → archivo local
    ],
)
logger = logging.getLogger(__name__)
```
Configura el sistema de logs. Todos los mensajes van a dos destinos:
1. **stdout** → PM2 lo captura en `logs/out.log`
2. **facturador.log** → archivo en la carpeta del proyecto con codificación UTF-8

El formato incluye fecha, hora, nivel (INFO/WARNING/ERROR) y el mensaje.

---

### `formatear_decimal(valor)`

```python
def formatear_decimal(valor) -> Decimal:
    if valor is None:
        return Decimal("0.00")
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")
```
Convierte cualquier valor (número, string, None) a un `Decimal` con exactamente 2 decimales. Usa redondeo ROUND_HALF_UP (el estándar contable: 0.005 sube a 0.01). Si el valor no se puede convertir, devuelve `0.00`.

---

### Líneas 106–114 — `formatear_fecha_hora(fecha_raw)`

```python
def formatear_fecha_hora(fecha_raw) -> datetime:
    if isinstance(fecha_raw, datetime):
        return fecha_raw
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(fecha_raw).strip(), fmt)
        except (ValueError, AttributeError):
            pass
    raise ValueError(f"Fecha inválida: {fecha_raw!r}")
```
Convierte fechas en diferentes formatos a un objeto `datetime`. Prueba 4 formatos distintos porque las fechas pueden venir de diferentes campos de SQL Server. Si ningún formato funciona, lanza un error.

---

### Líneas 117–121 — `escribir_archivo(ruta, contenido)`

```python
def escribir_archivo(ruta: str, contenido: str):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(contenido)
    os.replace(tmp, ruta)
```
Escribe un archivo de forma segura: primero escribe en un archivo temporal `.tmp` y luego lo renombra al nombre final. Esto evita que el SFS lea un archivo a mitad de escritura (operación atómica).

---

### Líneas 124–129 — `_mover(ruta, carpeta)`

```python
def _mover(ruta: str, carpeta: str):
    os.makedirs(carpeta, exist_ok=True)
    try:
        os.replace(ruta, os.path.join(carpeta, os.path.basename(ruta)))
    except Exception:
        logger.exception("No se pudo mover %s a %s", ruta, carpeta)
```
Mueve un archivo a una carpeta. Crea la carpeta si no existe. Si falla el movimiento (por ejemplo, el archivo está bloqueado), registra el error pero no detiene el programa.

---

### Líneas 135–140 — `conectar_bd()`

```python
def conectar_bd():
    conn_str = (
        f"DRIVER={DB_CONFIG['driver']};SERVER={DB_CONFIG['server']};"
        f"DATABASE={DB_CONFIG['database']};Trusted_Connection={DB_CONFIG['trusted_connection']};"
    )
    return pyodbc.connect(conn_str, timeout=DB_CONFIG["timeout"])
```
Crea y devuelve una conexión a SQL Server usando los parámetros del `DB_CONFIG`. El `timeout=30` evita que el programa se quede colgado si el servidor no responde.

---

### `_fila_dict(cur, fila)`

```python
def _fila_dict(cur, fila):
    return dict(zip([d[0] for d in cur.description], fila))
```
Helper que convierte una fila del cursor en un diccionario `{nombre_columna: valor}`. Se usa en todas las funciones de consulta para acceder a los datos por nombre en lugar de por índice.

---

### `obtener_emisor(conn)`

```python
def obtener_emisor(conn):
    cur = conn.cursor()
    cur.execute("SELECT TOP 1 ruc, razon_social FROM Emisores")
    fila = cur.fetchone()
    return _fila_dict(cur, fila) if fila else None
```
Lee el RUC y razón social del emisor desde la tabla `Emisores` de SQL Server. Devuelve el primer registro como diccionario, o `None` si la tabla está vacía.

---

### `obtener_receptor(conn, receptor_id)`

```python
def obtener_receptor(conn, receptor_id):
    if receptor_id is None:
        return {}
    cur = conn.cursor()
    cur.execute("SELECT TOP 1 * FROM Receptores WHERE id = ?", (int(receptor_id),))
    fila = cur.fetchone()
    return _fila_dict(cur, fila) if fila else {}
```
Busca los datos del cliente (receptor) en la tabla `Receptores` por su ID. Si no hay ID o no se encuentra, devuelve un diccionario vacío (el comprobante se generará con datos genéricos).

---

### `obtener_items(conn, comprobante_id)`

```python
def obtener_items(conn, comprobante_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM Items WHERE ComprobanteId = ?", (int(comprobante_id),))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, fila)) for fila in cur.fetchall()]
```
Obtiene todas las líneas de detalle (productos/servicios) del comprobante desde la tabla `Items`. Devuelve una lista de diccionarios.

---

### `obtener_comprobantes_pendientes(conn)`

```python
def obtener_comprobantes_pendientes(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM Comprobantes WHERE enviado IS NULL OR enviado = 0 ORDER BY fecha_emision ASC")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, fila)) for fila in cur.fetchall()]
```
Lee todos los comprobantes que aún no han sido enviados (`enviado = 0` o `NULL`), ordenados del más antiguo al más nuevo. Devuelve una lista de diccionarios.

---

### Líneas 175–178 — `_nombre_base(ruc, tipo, num)`

```python
def _nombre_base(ruc: str, tipo: str, num: str) -> str:
    serie, corr = num.split("-", 1) if "-" in num else ("0000", num or "00000000")
    nombre = f"{ruc}-{tipo}-{serie}-{corr}"
    return re.sub(r'[<>:"/\\|?*\n\r\t]+', "_", nombre.strip())[:250]
```
Construye el nombre base de los archivos SFS. Ejemplo: `20480072872-01-F001-000106`. Separa la numeración en serie y correlativo. Limpia caracteres inválidos para nombres de archivo y limita a 250 caracteres.

---

### Líneas 181–251 — `procesar_comprobante(conn, comp, ruc_emisor)`

Es la función principal de generación. Crea los 5 archivos que necesita el SFS para procesar un comprobante:

**Líneas 182–188 — Preparación**
```python
comp_id   = comp["id"]
num_comp  = str(comp.get("numeracion_comprobante", "")).strip()
tipo_comp = str(comp.get("tipo_comprobante", "")).strip() or "01"
base  = _nombre_base(ruc_emisor, tipo_comp, num_comp)
os.makedirs(SFS_DATA_DIR, exist_ok=True)
rutas = {e: os.path.join(SFS_DATA_DIR, f"{base}.{e}") for e in ("cab", "det", "tri", "ley", "PAG")}
```
Define el ID, numeración y tipo del comprobante. Crea un diccionario con las rutas de los 5 archivos.

**Verificar si ya fue procesado**
```python
enviado = comp.get("enviado")
faltan  = any(not os.path.exists(rutas[e]) for e in ("cab", "det", "tri", "ley"))
if not (enviado is None or enviado == 0 or faltan):
    return False
```
Si el comprobante ya fue enviado Y los archivos ya existen, no hace nada y retorna `False`.

**Líneas 195–203 — Datos del receptor y fechas**
Obtiene nombre, tipo/número de documento del receptor y convierte la fecha de emisión al formato requerido por el SFS (`YYYY-MM-DD` y `HH:MM:SS`).

**Líneas 205–207 — Totales**
```python
tot_grav  = formatear_decimal(comp.get("gravadas")  or comp.get("total_gravadas"))
tot_igv   = formatear_decimal(comp.get("igv")       or comp.get("total_igv"))
tot_venta = formatear_decimal(comp.get("total")     or comp.get("total_venta"))
```
Lee los montos del comprobante probando nombres alternativos de columna (por si la BD usa nombres diferentes).

**Líneas 210–227 — Líneas de detalle**
Itera cada ítem del comprobante y construye una línea de texto con el formato pipe `|` que espera el SFS:
`unidad|cantidad|codigo|-|descripcion|valor_unitario|igv|...|precio|valor_venta|...`

**Líneas 234–246 — Escritura de archivos**
- `.cab`: cabecera del comprobante (fecha, cliente, montos totales)
- `.PAG`: forma de pago (Contado/Crédito y monto)
- `.tri`: tributos (base imponible e IGV)
- `.ley`: leyenda (monto en letras)
- `.det`: detalle de ítems

**Líneas 248–250 — Marcar como enviado**
```python
conn.cursor().execute("UPDATE Comprobantes SET enviado = 1 WHERE id = ?", (int(comp_id),))
conn.commit()
```
Marca el comprobante como `enviado = 1` en SQL Server para que no se procese dos veces.

---

### Líneas 257–273 — `_sfs_post(path, payload)`

```python
def _sfs_post(path: str, payload: dict):
    url = f"{SFS_BASE_URL}/{path}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), ...)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.URLError as e:
        logger.warning("SFS no disponible (%s): %s", url, e)
        return None
```
Hace una llamada HTTP POST a la API del SFS local. Si el SFS no está disponible o hay error, registra el problema y devuelve `None` sin detener el programa.

---

### Líneas 276–309 — `activar_procesamiento_sfs(documentos)`

Envía los documentos al SFS para que los procese y los envíe a SUNAT.

**Líneas 279–283 — Verificar que el SFS está activo**
```python
urllib.request.urlopen(f"{SFS_BASE_URL}/", timeout=3)
```
Hace un ping rápido al SFS. Si no responde, no intenta enviar nada.

**Líneas 285–309 — Por cada documento**
1. Llama a `GenerarComprobante.htm` → el SFS convierte los archivos `.cab/.det/...` en XML firmado
2. Espera 2 segundos para dar tiempo al SFS
3. Llama a `enviarXML.htm` → el SFS envía el XML a SUNAT
4. Si falla el envío, espera 3 segundos y reintenta una vez más

---

### Líneas 315–335 — `_registrar_en_sfs_bd(ruc_emisor, docs)`

```python
def _registrar_en_sfs_bd(ruc_emisor: str, docs: list):
```
Registra los documentos generados en la BD SQLite del SFS (`BDFacturador.db`). Si el documento ya existe en la BD, no lo inserta de nuevo. Espera 2 segundos antes de escribir para que el SFS haya terminado de registrarlo primero.

---

### Líneas 338–340 — `_tiene_cdr(ruc, tip, num)`

```python
def _tiene_cdr(ruc: str, tip: str, num: str) -> bool:
    nombre = f"R{ruc}-{tip}-{num}.zip"
    return any(os.path.exists(os.path.join(d, nombre)) for d in (SFS_RPTA_DIR, DIR_PROCESADOS))
```
Verifica si el CDR (respuesta de SUNAT) ya llegó para un comprobante específico. Busca el archivo ZIP tanto en la carpeta RPTA como en procesados.

---

### Líneas 343–350 — `_eliminar_data_files(nom_arch)`

```python
def _eliminar_data_files(nom_arch: str):
    for ext in ("cab", "det", "tri", "ley", "PAG", "RDI", "TRD", "DET"):
        ruta = os.path.join(SFS_DATA_DIR, f"{nom_arch}.{ext}")
        if os.path.exists(ruta):
            os.remove(ruta)
```
Elimina los archivos temporales de DATA una vez que el comprobante fue aceptado. Esto limpia la carpeta DATA y evita que el SFS los reprocese.

---

### Líneas 353–378 — `_activar_pendientes_sfs_bd(ruc_emisor, ya_procesados)`

Revisa la BD del SFS buscando comprobantes en estado pendiente (`IND_SITU IN ('01','02')`) que no se procesaron en el ciclo actual:
- Si ya llegó su CDR → los marca como aceptados en la BD del SFS y elimina sus archivos DATA
- Si no llegó CDR → los reenvía al SFS llamando a `activar_procesamiento_sfs`

---

### Líneas 381–402 — `resetear_rechazados(conn, ruc_emisor)`

```python
def resetear_rechazados(conn, ruc_emisor: str):
```
Al inicio de cada ciclo, revisa si hay comprobantes con estado de error (`IND_SITU IN ('05','10')`) en la BD del SFS. Los comprobantes con error se:
1. Resetean a `enviado = 0` en SQL Server (para que se reintenten)
2. Eliminan de la BD del SFS (para empezar de cero)

---


### Líneas 525–528 — `_iter_elementos(elem, ancs)`

```python
def _iter_elementos(elem, ancs=()):
    yield elem, ancs
    for hijo in elem:
        yield from _iter_elementos(hijo, ancs + (elem.tag.split("}")[-1],))
```
Recorre todos los elementos de un XML de forma recursiva, llevando registro de los elementos ancestros. Esto permite saber en qué contexto está cada campo del CDR.

---

### Líneas 531–533 — `_extraer_numeracion(texto)`

```python
def _extraer_numeracion(texto) -> str | None:
    m = re.search(r"[A-Z]{1,3}\d{3}-\d+", str(texto or ""))
    return m.group(0) if m else None
```
Busca un patrón de numeración SUNAT (`F001-000106`, `B001-000001`, etc.) en un texto. Usa expresión regular para encontrarlo aunque esté mezclado con otro texto.

---

### Líneas 536–584 — `parsear_xml_cdr(fuente)`

Analiza el XML dentro del CDR (respuesta de SUNAT) y extrae:
- `numeracion`: número del comprobante (ej: `F001-000106`)
- `codigo`: código de respuesta de SUNAT (`0` = aceptado)
- `descripcion`: mensaje de SUNAT
- `status`: `ACEPTADO`, `RECHAZADO`, `OBSERVADO` o `ERROR`

El XML del CDR tiene namespaces complejos, por eso usa `_iter_elementos` para recorrerlo ignorando los prefijos de namespace.

---

### Líneas 587–600 — `_actualizar_sql_cdr(conn, numeracion, parsed)`

```python
def _actualizar_sql_cdr(conn, numeracion: str, parsed: dict) -> bool:
```
Si el CDR fue ACEPTADO, actualiza `enviado = 1` en la tabla `Comprobantes` de SQL Server. Devuelve `True` si actualizó alguna fila, `False` si no encontró el comprobante o no era ACEPTADO.

---

### Líneas 603–651 — `procesar_respuestas()`

Procesa todos los ZIPs y XMLs que hay en la carpeta RPTA:

1. Lista los archivos `.zip` y `.xml` en RPTA
2. Por cada archivo:
   - Si es ZIP: lo abre, extrae el XML interno y lo parsea
   - Si es XML: lo parsea directamente
3. Actualiza `enviado = 1` en SQL Server si fue ACEPTADO
4. Mueve el archivo a `procesados/` si todo fue bien, o a `errores/` si el ZIP estaba corrupto

---

### Líneas 657–697 — `ciclo_generacion()`

Es el ciclo completo que se ejecuta cada 60 segundos:

1. **Conecta a SQL Server**
2. **Lee el emisor** (RUC y razón social)
3. **Resetea rechazados** — comprobantes con error en el SFS vuelven a `enviado = 0`
4. **Lee comprobantes pendientes** de SQL Server
5. **Genera archivos SFS** para cada comprobante pendiente
6. **Envía al SFS** los comprobantes generados
7. **Registra en la BD del SFS** los documentos enviados
8. **Activa pendientes** — revisa si hay comprobantes viejos en el SFS que necesitan reenvío

---

### Líneas 703–707 — `hilo_generador()`

```python
def hilo_generador():
    logger.info("Hilo GENERADOR iniciado (intervalo: %ds)", INTERVALO_GENERACION_SEG)
    while True:
        ciclo_generacion()
        time.sleep(INTERVALO_GENERACION_SEG)
```
Loop infinito que ejecuta `ciclo_generacion()` y luego espera 60 segundos antes de repetir.

---

### Líneas 713–720 — `CDRHandler`

```python
class CDRHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.lower().endswith((".zip", ".xml")):
            logger.info("CDR detectado: %s", os.path.basename(event.src_path))
            time.sleep(1)
            procesar_respuestas()
```
Clase que reacciona cuando el sistema operativo detecta un archivo nuevo en la carpeta RPTA. Si es un ZIP o XML, espera 1 segundo (para que el SFS termine de escribirlo) y llama a `procesar_respuestas()`.

---

### Líneas 723–739 — `hilo_cdr()`

```python
def hilo_cdr():
    os.makedirs(SFS_RPTA_DIR, exist_ok=True)
    os.makedirs(DIR_PROCESADOS, exist_ok=True)
    os.makedirs(DIR_ERRORES,    exist_ok=True)
    observer = Observer()
    observer.schedule(handler, path=SFS_RPTA_DIR, recursive=False)
    observer.start()
    while True:
        time.sleep(10)
```
Crea las carpetas RPTA/procesados/errores si no existen. Inicia el `Observer` de watchdog que monitorea la carpeta RPTA. El `while True: sleep(10)` mantiene el hilo vivo.

---

### Líneas 745–766 — `if __name__ == "__main__"` — Punto de entrada

```python
if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  FACTURADOR SUNAT - v2.0")
    ...
```

1. **Imprime un banner** con las rutas y servidor configurados
2. **Llama a `procesar_respuestas()`** al arrancar — por si quedaron ZIPs pendientes de antes
3. **Crea el hilo Generador** con `daemon=True` (se detiene si el programa principal se cierra)
4. **Crea el hilo CDR** con `daemon=True`
5. **Inicia ambos hilos**
6. **Entra en loop infinito** `sleep(60)` esperando Ctrl+C para detener limpiamente

---

## Flujo completo de un comprobante

```
SQL Server (enviado=0)
        ↓
ciclo_generacion() cada 60s
        ↓
procesar_comprobante() → genera .cab .det .tri .ley .PAG en DATA
        ↓  (enviado=1 en SQL Server)
activar_procesamiento_sfs()
        ↓
SFS: GenerarComprobante → crea XML firmado
        ↓
SFS: enviarXML → envía a SUNAT
        ↓
SUNAT → devuelve CDR (ZIP) en carpeta RPTA
        ↓
CDRHandler detecta ZIP (watchdog, tiempo real)
        ↓
procesar_respuestas() → parsea XML del CDR
        ↓
Si ACEPTADO → enviado=1 confirmado en SQL Server
              ZIP movido a RPTA/procesados/
              Archivos DATA eliminados
```

---

## Archivos que intervienen

| Archivo | Descripción |
|---|---|
| `main.py` | El daemon (este archivo) |
| `.env` | Configuración (rutas, RUC, servidor SQL) |
| `sfs.config.js` | Configuración de PM2 |
| `facturador.log` | Log local del daemon |
| `BDFacturador.db` | BD SQLite interna del SFS |
| `DATA/*.cab` | Cabecera del comprobante |
| `DATA/*.det` | Detalle de ítems |
| `DATA/*.tri` | Tributos (IGV) |
| `DATA/*.ley` | Leyenda (monto en letras) |
| `DATA/*.PAG` | Forma de pago |
| `RPTA/*.zip` | CDR de SUNAT (respuesta) |
| `RPTA/procesados/` | CDRs procesados correctamente |
| `RPTA/errores/` | CDRs con problemas |
