# Explicación del Código – Línea a Línea
**Robot SUNAT v1.0**

---

## Archivo: `generador_sfs.py`

Este archivo es el motor del sistema. Se encarga de leer la base de datos, generar los archivos DATA y comunicarse con el SFS.

---

### SECCIÓN 1 – Importaciones y configuración (líneas 1–51)

```python
import json, logging, os, re, sqlite3, time, urllib.request
from decimal import Decimal, ROUND_HALF_UP
import pandas as pd
import pyodbc
```
**¿Qué hace?** Carga las librerías necesarias:
- `sqlite3` → para leer/escribir la base de datos del SFS (BDFacturador.db)
- `pyodbc` → para conectarse a SQL Server (base de datos del negocio)
- `pandas` → para leer tablas de SQL Server como si fueran hojas de cálculo
- `decimal` → para hacer cálculos de dinero sin errores de redondeo
- `urllib.request` → para llamar a la API REST del SFS (puerto 9000)

---

```python
_SFS_DATA = r"C:\SFS_v2.1\sunat_archivos\sfs\DATA"
FACTURADOR_SUNAT_IMPORT_DIR = (
    os.getenv("FACTURADOR_SUNAT_IMPORT_DIR", "").strip()
    or (_SFS_DATA if os.path.exists(_SFS_DATA) else ...)
)
```
**¿Qué hace?** Define dónde escribir los archivos DATA que el SFS va a leer.
Primero busca una variable de entorno (`FACTURADOR_SUNAT_IMPORT_DIR`); si no existe, usa la carpeta por defecto de instalación del SFS.

---

```python
DB_CONFIG = {
    "driver": "{SQL Server}",
    "server": r".\SQLEXPRESS",
    "database": "AUXILIAR",
    "trusted_connection": "yes",
    "timeout": 30,
}
```
**¿Qué hace?** Configuración de conexión a SQL Server. `Trusted_Connection=yes` significa que usa la sesión de Windows actual (sin usuario/contraseña explícita).

---

```python
SFS_BD_PATH  = r"C:\SFS_v-2.1\bd\BDFacturador.db"
SFS_BASE_URL = "http://localhost:9000"
_TIPOS_SFS   = {'01', '03', '07', '08', 'RC', 'RA'}
```
**¿Qué hace?**
- `SFS_BD_PATH` → ruta a la base de datos SQLite interna del SFS
- `SFS_BASE_URL` → dirección del SFS corriendo localmente
- `_TIPOS_SFS` → lista de tipos que el SFS sabe procesar; los demás (50, 51, 52) se omiten

---

### SECCIÓN 2 – Funciones de utilidad (líneas 58–137)

#### `conectar_bd()`
```python
def conectar_bd():
    conn_str = f"DRIVER=...;SERVER=...;DATABASE=AUXILIAR;..."
    return pyodbc.connect(conn_str, timeout=30)
```
**¿Qué hace?** Abre una conexión a SQL Server y la devuelve. Se llama al inicio de cada operación que necesita leer datos del negocio.

---

#### `formatear_decimal(valor)`
```python
def formatear_decimal(valor):
    return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
```
**¿Qué hace?** Convierte cualquier número a decimal con exactamente 2 decimales, redondeando correctamente (ej: 10.005 → 10.01). Se usa en todos los cálculos de importes para evitar errores de centavos que rechaza SUNAT.

---

#### `numero_a_letras(monto, moneda)`
```python
def numero_a_letras(monto, moneda='PEN'):
    # ... lógica de conversión ...
    return f'SON {_entero(entero)} CON {centavos:02d}/100 {nombre_mon}'
```
**¿Qué hace?** Convierte un número a texto para el archivo `.ley`.
Ejemplo: `150.50` → `"SON CIENTO CINCUENTA CON 50/100 SOLES"`
SUNAT exige este campo en el XML del comprobante.

---

#### `escribir_archivo(ruta, contenido)`
```python
def escribir_archivo(ruta, contenido):
    tmp = ruta + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(contenido)
    os.replace(tmp, ruta)  # reemplazo atómico
```
**¿Qué hace?** Escribe un archivo DATA de forma segura: primero escribe en un archivo temporal (`.tmp`) y luego lo renombra al nombre final. Así, si el programa se interrumpe a mitad de escritura, no queda un archivo corrupto.

---

### SECCIÓN 3 – Acceso a SQL Server (líneas 144–162)

#### `obtener_emisor(conn)`
```python
df = pd.read_sql("SELECT TOP 1 ruc, razon_social FROM Emisores", conn)
return df.iloc[0].to_dict()
```
**¿Qué hace?** Lee el RUC y razón social de la empresa emisora desde SQL Server. Se usa para armar los nombres de archivos y los encabezados de los comprobantes.

---

#### `obtener_receptor(conn, receptor_id)`
```python
df = pd.read_sql("SELECT TOP 1 * FROM Receptores WHERE id = ?", conn, params=(receptor_id,))
```
**¿Qué hace?** Lee los datos del cliente (tipo de documento, número, razón social) para un comprobante específico.

---

#### `obtener_comprobantes(conn, only_pending=True)`
```python
filtro = "WHERE enviado IS NULL OR enviado = 0" if only_pending else ""
return pd.read_sql(f"SELECT * FROM Comprobantes {filtro} ...", conn)
```
**¿Qué hace?** Lee los comprobantes de SQL Server. Cuando `only_pending=True` solo trae los que tienen `enviado = 0` (pendientes). Esta es la consulta que decide qué comprobantes hay que enviar a SUNAT.

---

### SECCIÓN 4 – Generación de archivos DATA (líneas 169–266)

#### `_nombre_base(ruc, tipo, num)`
```python
serie, corr = num.split('-', 1)   # "F001-000053" → ("F001", "000053")
return f"{ruc}-{tipo}-{serie}-{corr}"
# resultado: "20480072872-01-F001-000053"
```
**¿Qué hace?** Construye el nombre base de los archivos DATA. El SFS exige este formato exacto para encontrar los archivos.

---

#### `procesar_comprobante(conn, cursor, comp, ruc_emisor)` ← función central
Esta es la función más importante del archivo. Por cada comprobante genera los 5 archivos DATA.

```python
enviado = comp.get('enviado')
faltan  = any(not os.path.exists(rutas[e]) for e in ('cab', 'det', 'tri', 'ley'))
if not (pd.isna(enviado) or enviado == 0 or faltan):
    return False   # ya fue procesado y los archivos existen → saltar
```
**¿Qué hace?** Verifica si el comprobante ya fue procesado. Si `enviado=1` Y los archivos ya existen en disco, no hace nada y pasa al siguiente.

---

```python
df_items = obtener_items(conn, comp_id)
for _, item in df_items.iterrows():
    cant  = formatear_decimal(item.get('cantidad', 1))
    v_vta = v_vta_db * cant   # si valor_venta ≈ valor_unitario, multiplica por cantidad
    igv   = v_vta * Decimal("0.18")
    ...
    tot_grav += v_vta
    tot_igv  += igv
```
**¿Qué hace?** Calcula los totales recorriendo cada ítem. Recalcula desde los ítems (no usa el total del comprobante) porque SUNAT verifica que la suma de ítems cuadre con el total; si no cuadra, devuelve errores 3291 o 3277.

---

```python
escribir_archivo(rutas['cab'],
    f"0101|{fecha_str}|{hora_str}|-|0000|{tipo_doc_rec}|{num_doc_rec}|"
    f"{razon_social}|{moneda}|{tot_igv:.2f}|{tot_grav:.2f}|{tot_venta:.2f}|..."
)
escribir_archivo(rutas['tri'], f"1000|IGV|VAT|{tot_grav:.2f}|{tot_igv:.2f}|\n")
escribir_archivo(rutas['ley'], f"1000|{monto_letras}|\n")
escribir_archivo(rutas['PAG'], f"Contado|{tot_venta:.2f}|{moneda}|\n")
escribir_archivo(rutas['det'], ''.join(lineas_det))
```
**¿Qué hace?** Escribe los 5 archivos DATA en formato pipe (`|`):
- `.cab` → cabecera: versión, fecha, receptor, totales
- `.tri` → tributos: código IGV, base imponible, monto IGV
- `.ley` → monto en letras
- `.PAG` → forma de pago
- `.det` → una línea por cada ítem del comprobante

---

```python
cursor.execute("UPDATE Comprobantes SET enviado = 1 WHERE id = ?", (int(comp_id),))
conn.commit()
```
**¿Qué hace?** Marca el comprobante como `enviado=1` en SQL Server para que en la próxima ejecución no lo vuelva a procesar.

---

### SECCIÓN 5 – Comunicación con el SFS (líneas 273–399)

#### `_sfs_post(path, payload)`
```python
req = urllib.request.Request(
    url, data=json.dumps(payload).encode(),
    headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
)
with urllib.request.urlopen(req, timeout=30) as r:
    return json.loads(r.read().decode())
```
**¿Qué hace?** Envía una llamada HTTP POST al SFS (puerto 9000) con un JSON como cuerpo. Devuelve la respuesta del SFS también como JSON. Es la función base para todas las llamadas a la API del SFS.

---

#### `_registrar_en_sfs_bd(ruc_emisor, docs)`
```python
existe = sfs.execute("SELECT 1 FROM DOCUMENTO WHERE ...").fetchone()
if not existe:
    sfs.execute("INSERT INTO DOCUMENTO (..., IND_SITU, DES_OBSE) VALUES (..., '03', 'Aceptado por SUNAT')")
```
**¿Qué hace?** Inserta en la base de datos SQLite del SFS los documentos que fueron aceptados tan rápido que el SFS no alcanzó a registrarlos (caso de facturas aceptadas en milisegundos). Los inserta directamente con estado `'03'` (Aceptado).

---

#### `_tiene_cdr(ruc, tip, num)`
```python
nombre = f"R{ruc}-{tip}-{num}.zip"
return (
    os.path.exists(os.path.join(SFS_RPTA_DIR, nombre)) or
    os.path.exists(os.path.join(SFS_RPTA_DIR, "procesados", nombre))
)
```
**¿Qué hace?** Verifica si ya existe el CDR (respuesta ZIP de SUNAT) para un documento, buscando en la carpeta RPTA y en la subcarpeta `procesados`. Esto evita reenviar documentos que ya fueron aceptados.

---

#### `_eliminar_data_files(nom_arch)`
```python
for ext in ('cab', 'det', 'tri', 'ley', 'PAG', 'RDI', 'TRD', 'DET'):
    ruta = os.path.join(FACTURADOR_SUNAT_IMPORT_DIR, f"{nom_arch}.{ext}")
    if os.path.exists(ruta): os.remove(ruta)
```
**¿Qué hace?** Elimina todos los archivos DATA de un documento ya aceptado. Esto es importante porque el SFS tiene una tarea en background que escanea la carpeta DATA y re-carga cualquier archivo que encuentre. Si no se eliminan, el SFS intenta re-enviar documentos ya aceptados.

---

#### `_activar_pendientes_sfs_bd(ruc_emisor, ya_procesados)` ← función de recuperación
```python
rows = sfs.execute(
    "SELECT TIP_DOCU, NUM_DOCU, NOM_ARCH FROM DOCUMENTO "
    "WHERE NUM_RUC=? AND TIP_DOCU IN ('01','03',...) AND IND_SITU IN ('01','02')",
    ...
).fetchall()
```
**¿Qué hace?** Busca en el SFS BD los documentos que están en estado `'01'` (cargados pero sin XML) o `'02'` (XML generado pero no enviado).

```python
if _tiene_cdr(ruc_emisor, tip, num):
    sfs.execute("UPDATE DOCUMENTO SET IND_SITU='03' ...")
    _eliminar_data_files(nom_arch)
    continue
docs_extra.append(...)
```
**¿Qué hace?** Para cada uno decide:
- **Si ya tiene CDR** → el documento ya fue aceptado antes; corrige el estado a `'03'` y elimina los DATA files para evitar re-envíos
- **Si NO tiene CDR** → el documento genuinamente no fue enviado; lo agrega a la lista para activar

```python
if docs_extra:
    activar_procesamiento_sfs(docs_extra)
```
**¿Qué hace?** Llama al SFS para que procese los documentos pendientes. Esto resuelve el caso donde el SFS cargó documentos en su bandeja (`'01'`) pero su timer interno no alcanzó a procesarlos.

---

#### `activar_procesamiento_sfs(documentos)`
```python
urllib.request.urlopen(f"{SFS_BASE_URL}/", timeout=3)  # verifica que el SFS esté vivo
```
**¿Qué hace?** Primero verifica que el SFS esté respondiendo. Si no responde, no intenta nada y muestra advertencia.

```python
r1 = _sfs_post("api/GenerarComprobante.htm", payload)
if not (r1 and r1.get("validacion") == "EXITO"):
    continue   # si falla, pasa al siguiente
time.sleep(2)
r2 = _sfs_post("api/enviarXML.htm", payload)
```
**¿Qué hace?** Por cada documento hace dos llamadas al SFS:
1. **`GenerarComprobante`** → el SFS lee los DATA files, genera el XML UBL y lo firma digitalmente (estado `'01'` → `'02'`)
2. **`enviarXML`** → el SFS envía el XML firmado a SUNAT (estado `'02'` → `'03'` para facturas, o `'02'` → `'08'` para RCs)

```python
time.sleep(3)
r2 = _sfs_post("api/enviarXML.htm", payload)  # reintento
```
**¿Qué hace?** Si `enviarXML` falla en el primer intento, espera 3 segundos y reintenta una vez más antes de registrar el error.

---

### SECCIÓN 6 – Resumen Diario de Boletas RC (líneas 406–521)

#### `generar_rc_boletas_viejas(ruc_emisor)`

```python
rows = sfs.execute(
    "SELECT NUM_DOCU, NOM_ARCH FROM DOCUMENTO "
    "WHERE NUM_RUC=? AND TIP_DOCU='03' AND IND_SITU='06' "
    "AND (DES_OBSE LIKE '%mas de 5%' OR DES_OBSE LIKE '%más de 5%')",
    ...
).fetchall()
```
**¿Qué hace?** Busca en el SFS BD las boletas que el SFS marcó como `'06'` (más de 5 días). Estas no pueden enviarse individualmente; SUNAT exige agruparlas en un Resumen Diario.

---

```python
cab_p = open(base + '.cab').readline().strip().split('|')
tri_p = open(base + '.tri').readline().strip().split('|')
fec   = cab_p[1]    # fecha de emisión de la boleta
b_imp = tri_p[3]    # base imponible
igv   = tri_p[4]    # IGV
boletas_por_fecha.setdefault(fec, []).append({...})
```
**¿Qué hace?** Lee los archivos DATA de cada boleta para extraer sus importes y agrupa las boletas por fecha de emisión. SUNAT exige un RC separado por cada fecha.

---

```python
for fec in sorted(boletas_por_fecha):
    rc_num  = f"RC-{today_str}-{seq}"   # ej: "RC-20260622-1"
    rc_arch = f"{ruc_emisor}-{rc_num}"  # ej: "20480072872-RC-20260622-1"
    seq += 1
```
**¿Qué hace?** Por cada fecha con boletas, crea un RC nuevo con número secuencial del día. Si ya se crearon 2 RCs hoy, el siguiente será RC-...-3.

---

```python
rdi = '\n'.join(
    f"{fec}|{today_iso}|03|{b['num']}|{b['tip']}|{b['doc']}|"
    f"PEN|{b['base']}|0.00|...|{b['tot']}|...|1|"
    for i, b in enumerate(boletas, 1)
) + '\n'
```
**¿Qué hace?** Genera el archivo `.RDI` con una línea por boleta. El campo 4 (índice 3) es el número de boleta (`B001-990212`), que es lo que SUNAT valida en el XML del RC.

---

```python
sfs.execute(
    "INSERT INTO DOCUMENTO (..., IND_SITU, TIP_ARCH) VALUES (?,?,?,?,?,?)",
    (ruc_emisor, 'RC', rc_num, rc_arch, '01', 'TXT')
)
```
**¿Qué hace?** Registra el RC en la bandeja del SFS con `TIP_ARCH='TXT'`. Este valor es **crítico**: sin él, el SFS no genera el XML del RC y SUNAT rechaza con error "ZIP vacío".

---

```python
for b in boletas:
    sfs.execute("UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE=? ...", (f"Aceptado via {rc_num}", ...))
    _eliminar_data_files(nom_boleta)
```
**¿Qué hace?** Marca cada boleta incluida en el RC como `'03'` (aceptada) en el SFS BD y elimina sus DATA files. Esto evita que el SFS la incluya en un segundo RC en el próximo ciclo.

---

### SECCIÓN 7 – Flujo principal (líneas 524–608)

#### `resetear_rechazados_en_sfs_bd(conn_aux, ruc_emisor)`
```python
rows = sfs.execute(
    "SELECT NUM_DOCU, TIP_DOCU FROM DOCUMENTO WHERE IND_SITU IN ('05','10')", ...
).fetchall()
for num_docu, tip_docu in rows:
    cur.execute("UPDATE Comprobantes SET enviado=0 WHERE numeracion_comprobante=?", ...)
sfs.execute("DELETE FROM DOCUMENTO WHERE IND_SITU IN ('05','10')", ...)
```
**¿Qué hace?** Al inicio de cada sincronización, busca documentos rechazados o con error (`'05'` o `'10'`) en el SFS, los borra de la bandeja del SFS y pone `enviado=0` en SQL Server para que se reintenten en este ciclo.

---

#### `generar_archivos_sfs(only_pending=True)` ← función orquestadora

```python
resetear_rechazados_en_sfs_bd(conn, ruc_emisor)   # 1. Limpia rechazados
df_comp = obtener_comprobantes(conn, only_pending)  # 2. Lee pendientes
```

```python
if df_comp.empty:
    _activar_pendientes_sfs_bd(ruc_emisor, [])  # 3a. Si no hay nuevos:
    generar_rc_boletas_viejas(ruc_emisor)        #     activa pendientes y genera RC
    return
```
**¿Qué hace?** Si no hay comprobantes nuevos en SQL Server (`enviado=0`), igual revisa si el SFS tiene documentos en `'01'` que necesiten activación, y genera los RCs de boletas viejas.

```python
for _, comp in df_comp.iterrows():
    if procesar_comprobante(conn, cursor, comp, ruc_emisor):
        docs_generados.append({...})
```
**¿Qué hace?** Recorre cada comprobante pendiente y genera sus archivos DATA. Los que se procesaron exitosamente quedan en `docs_generados`.

```python
activar_procesamiento_sfs(docs_generados)   # 4. Llama al SFS para generar XML y enviar
_registrar_en_sfs_bd(ruc_emisor, docs_generados)  # 5. Registra en SFS BD los que el SFS no registró
_activar_pendientes_sfs_bd(ruc_emisor, docs_generados)  # 6. Activa cualquier '01' rezagado
generar_rc_boletas_viejas(ruc_emisor)       # 7. Genera RCs para boletas viejas
```

---

## Archivo: `app_consola.py`

Este archivo maneja la interfaz de usuario (menú de consola) y el procesamiento de las respuestas CDR de SUNAT.

---

### SECCIÓN 1 – Parseo de CDR XML (líneas 58–117)

#### `parsear_xml_cdr(ruta_xml)`
```python
for elem, ancs in _iter_elementos(root):
    tag = elem.tag.split('}')[-1].lower()
    if tag == "responsecode":
        res["codigo"] = text          # ej: "0" = aceptado
    elif tag == "referenceid":
        res["numeracion"] = _extraer_numeracion(text)  # ej: "F001-000053"
    elif tag in {"description", "responsedescription"}:
        descripciones.append(text)    # ej: "La Factura numero F001-000053, ha sido aceptada"
```
**¿Qué hace?** Abre el XML dentro del ZIP de respuesta SUNAT y extrae tres datos:
- **código** de respuesta (0 = aceptado, otro = error)
- **numeración** del comprobante (para saber a cuál corresponde)
- **descripción** del resultado

```python
if "acept" in desc or codigo in {"0", "01", "0000"}:
    res["status"] = "ACEPTADO"
elif "rechaz" in desc or "error" in desc:
    res["status"] = "RECHAZADO"
```
**¿Qué hace?** Determina el estado final buscando palabras clave en la descripción o en el código de respuesta.

---

### SECCIÓN 2 – Actualización en SQL Server (líneas 128–160)

#### `_actualizar_sql(conn, numeracion, parsed, origen)`
```python
df = pd.read_sql(
    "SELECT TOP 1 * FROM Comprobantes WHERE numeracion_comprobante = ?",
    conn, params=(numeracion,)
)
if df.empty:
    return False   # el CDR no corresponde a ningún comprobante conocido (ej: RC)
```
**¿Qué hace?** Busca el comprobante en SQL Server por su número (ej: `F001-000053`). Si no lo encuentra (caso de RCs), retorna False sin hacer nada.

```python
if "enviado" in cols and parsed["status"] == "ACEPTADO":
    upd.append("enviado = ?"); params.append(1)
```
**¿Qué hace?** Solo actualiza `enviado=1` si el estado es ACEPTADO. Si fue rechazado, deja `enviado` como estaba para que el próximo ciclo lo reintente (después de que `resetear_rechazados_en_sfs_bd` lo limpie del SFS BD).

---

### SECCIÓN 3 – Procesamiento de respuestas (líneas 167–239)

#### `procesar_respuestas()`
```python
archivos = [
    os.path.join(RESPUESTAS_DIR, f)
    for f in os.listdir(RESPUESTAS_DIR)
    if f.lower().endswith(('.zip', '.xml'))
]
```
**¿Qué hace?** Lista todos los archivos ZIP y XML en la carpeta RPTA. Solo busca en el nivel raíz, no en subcarpetas.

```python
with zipfile.ZipFile(ruta) as z:
    xml_names = [n for n in z.namelist() if n.lower().endswith('.xml')]
    xml_bytes = z.read(xml_names[0])
```
**¿Qué hace?** Abre el ZIP del CDR y extrae el XML de respuesta de SUNAT que contiene el código de aceptación/rechazo.

```python
parsed = parsear_xml_cdr(tmp_path)
num = parsed["numeracion"]
if _actualizar_sql(conn, num, parsed, nombre):
    ok += 1
_mover(ruta, DIR_PROCESADOS)
```
**¿Qué hace?** Parsea el XML, actualiza SQL Server si corresponde a un comprobante conocido, y mueve el ZIP a la subcarpeta `procesados/` para no procesarlo dos veces.

---

#### `_esperar_respuestas(timeout=90, intervalo=5)`
```python
fin = time.time() + timeout
while time.time() < fin:
    if any(f.lower().endswith(('.zip', '.xml')) for f in os.listdir(RESPUESTAS_DIR)):
        return True    # encontró CDRs → procesar
    time.sleep(intervalo)
    print("[i] Esperando...")
return False   # timeout: no llegaron CDRs en 90 segundos
```
**¿Qué hace?** Monitorea la carpeta RPTA cada 5 segundos por hasta 90 segundos. En cuanto detecta un ZIP o XML, retorna `True` inmediatamente. Si pasan 90 segundos sin CDRs, retorna `False`.

---

### SECCIÓN 4 – Sincronización (líneas 323–330)

#### `sincronizar_facturador()` ← función que llama la opción 5
```python
def sincronizar_facturador():
    procesar_respuestas()                        # Paso 1: CDRs existentes
    generador_sfs.generar_archivos_sfs(True)     # Paso 2: Genera y envía nuevos
    if _esperar_respuestas():                    # Paso 3: Espera hasta 90s
        procesar_respuestas()                    # Paso 4: CDRs recién llegados
    else:
        print("[!] Sin nuevos CDRs; revisa RPTA.")
```
**¿Qué hace?** Orquesta el ciclo completo en 4 pasos. Es lo que ejecuta la opción 5 del menú.

---

### SECCIÓN 5 – Menú (líneas 337–368)

#### `menu()`
```python
opciones = {
    '1': ("Leer comprobantes",               lambda: leer_comprobantes()),
    '2': ("Leer y descargar PDFs",           lambda: descargar_pdfs(leer_comprobantes())),
    '3': ("Generar archivos SFS pendientes", lambda: generador_sfs.generar_archivos_sfs(...)),
    '4': ("Procesar CDRs de SUNAT",          procesar_respuestas),
    '5': ("Sincronizar (generar + procesar)",sincronizar_facturador),
}
while True:
    opcion = input("Elige una opción (1-6): ").strip()
    opciones[opcion][1]()   # ejecuta la función correspondiente
```
**¿Qué hace?** Muestra el menú en pantalla y ejecuta la función correspondiente a la opción elegida. El bucle `while True` sigue mostrando el menú hasta que el usuario elige la opción 6 (Salir).

---

## Resumen de llamadas entre funciones

```
menu()
  └─ sincronizar_facturador()                    [opción 5]
        ├─ procesar_respuestas()                 Lee CDRs de RPTA y actualiza SQL Server
        │     └─ _actualizar_sql()               Actualiza enviado=1 si ACEPTADO
        │
        ├─ generar_archivos_sfs()                Motor principal
        │     ├─ resetear_rechazados_en_sfs_bd() Limpia rechazados para reintento
        │     ├─ obtener_comprobantes()           Lee SQL Server: enviado=0
        │     ├─ procesar_comprobante()           Genera .cab .det .tri .ley .PAG
        │     ├─ activar_procesamiento_sfs()      Llama GenerarComprobante + enviarXML
        │     ├─ _registrar_en_sfs_bd()           Registra en SQLite del SFS si faltó
        │     ├─ _activar_pendientes_sfs_bd()     Activa docs '01' rezagados
        │     │     └─ _tiene_cdr()              ¿Ya tiene CDR? → corrige estado
        │     │     └─ _eliminar_data_files()    Borra DATA files de aceptados
        │     └─ generar_rc_boletas_viejas()      Agrupa boletas '06' en RCs
        │           └─ activar_procesamiento_sfs() Envía RCs al SFS
        │
        ├─ _esperar_respuestas()                 Monitorea RPTA por 90 segundos
        └─ procesar_respuestas()                 Procesa CDRs que llegaron
```
