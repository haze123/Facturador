"""
Un CDR bajado por consulta cierra el mismo comprobante que uno de envio normal.

Corre sin base de datos ni SFS: arma los CDR y una BD SQLite temporal con la forma
de DOCUMENTO. Desde la raiz del proyecto:

    python pruebas/test_cdr_recuperado.py

Cubre los dos formatos de numeracion que usa SUNAT segun por donde llegue el CDR
(ver _reconciliar_numeracion), que el cierre de la bandeja sea idempotente, y que
un fallo al cerrar en la BD deje el error visible en vez de borrarlo.
"""
import os, sys, sqlite3, tempfile, zipfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SFS_DATA_DIR"] = tempfile.mkdtemp()
import main as m, logging
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp()
m.SFS_RPTA_DIR  = os.path.join(TMP, "RPTA");      os.makedirs(m.SFS_RPTA_DIR)
m.DIR_PROCESADOS = os.path.join(TMP, "procesados"); os.makedirs(m.DIR_PROCESADOS)
m.DIR_ERRORES    = os.path.join(TMP, "errores");    os.makedirs(m.DIR_ERRORES)
RUC = "20605858601"

FALLAS = 0
def check(c, msg):
    global FALLAS
    if not c: FALLAS += 1
    print(("  OK    " if c else "  FALLA "), msg)

CDR = """<?xml version="1.0" encoding="UTF-8"?>
<ApplicationResponse xmlns="urn:oasis:names:specification:ubl:schema:xsd:ApplicationResponse-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
  <cbc:ID>{ident}</cbc:ID>
  <cac:DocumentResponse><cac:Response>
    <cbc:ReferenceID>{ident}</cbc:ReferenceID>
    <cbc:ResponseCode>0</cbc:ResponseCode>
    <cbc:Description>El Comprobante {ident}, ha sido aceptado</cbc:Description>
  </cac:Response></cac:DocumentResponse>
</ApplicationResponse>"""

def poner_cdr(nombre, ident):
    ruta = os.path.join(m.SFS_RPTA_DIR, nombre)
    with zipfile.ZipFile(ruta, "w") as z:
        z.writestr(nombre.replace(".zip", ".xml"), CDR.format(ident=ident))
    return ruta

def bd_sfs(filas):
    ruta = os.path.join(TMP, f"sfs{time.time_ns()}.db")
    c = sqlite3.connect(ruta)
    c.execute("CREATE TABLE DOCUMENTO (NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, IND_SITU TEXT, FEC_ENVI TEXT, DES_OBSE TEXT)")
    c.executemany("INSERT INTO DOCUMENTO VALUES (?,?,?,?,?,?)", filas)
    c.commit(); c.close()
    m.SFS_BD_PATH = ruta
    return ruta

def fila(ruta, num):
    c = sqlite3.connect(ruta)
    r = c.execute("SELECT IND_SITU, FEC_ENVI, DES_OBSE FROM DOCUMENTO WHERE NUM_DOCU=?", (num,)).fetchone()
    c.close(); return r

CERRADOS = []
m.conectar_bd = lambda: None
m._actualizar_sql_cdr = lambda conn, num, parsed: (CERRADOS.append(num), True)[1] if num in EXISTEN else False

# ---- criterio 1: CDR de consulta cierra el comprobante con ceros ----
EXISTEN = {"F003-009571"}
CERRADOS.clear()
db = bd_sfs([(RUC, "01", "F003-009571", "06", None, "Could not send Message.")])
poner_cdr("R20605858601-01-F003-009571.zip", "20605858601-01-F003-9571")
m._barrer_rpta()
check(CERRADOS == ["F003-009571"], f"criterio 1: cierra F003-009571 (cerro {CERRADOS})")
situ, fec, obse = fila(db, "F003-009571")
check(situ == "03", f"criterio 4: la fila del SFS queda en '03' ({situ})")
check(fec and "/" in fec, f"criterio 4: FEC_ENVI cargada ({fec})")
check(obse == "-", f"criterio 4: DES_OBSE limpio ({obse!r})")
check(os.listdir(m.DIR_PROCESADOS) and not os.listdir(m.DIR_ERRORES), "el zip fue a procesados/")

# ---- criterio 2: el CDR normal sigue igual ----
EXISTEN = {"F003-009595"}
CERRADOS.clear()
for d in (m.DIR_PROCESADOS, m.DIR_ERRORES):
    for f in os.listdir(d): os.remove(os.path.join(d, f))
db = bd_sfs([(RUC, "01", "F003-009595", "03", "03/09/2026 09:58:56", "-")])
poner_cdr("R20605858601-01-F003-009595.zip", "F003-009595")
m._barrer_rpta()
check(CERRADOS == ["F003-009595"], f"criterio 2: el camino normal no se rompe ({CERRADOS})")

# ---- idempotencia: el mismo CDR barrido dos veces ----
EXISTEN = {"F003-009571"}
CERRADOS.clear()
db = bd_sfs([(RUC, "01", "F003-009571", "03", "03/09/2026 10:00:00", "-")])
poner_cdr("R20605858601-01-F003-009571.zip", "20605858601-01-F003-9571")
m._barrer_rpta()
situ2, fec2, _ = fila(db, "F003-009571")
check(situ2 == "03" and fec2 == "03/09/2026 10:00:00",
      f"idempotente: un segundo pase no revierte ni repisa ({situ2}, {fec2})")

# ---- criterio 5: si el cierre en la BD falla, la fila conserva su error ----
EXISTEN = set()
CERRADOS.clear()
for d in (m.DIR_PROCESADOS, m.DIR_ERRORES):
    for f in os.listdir(d): os.remove(os.path.join(d, f))
db = bd_sfs([(RUC, "01", "F003-009571", "06", None, "Could not send Message.")])
poner_cdr("R20605858601-01-F003-009571.zip", "20605858601-01-F003-9571")
m._barrer_rpta()
situ3, _, obse3 = fila(db, "F003-009571")
check(situ3 == "06", f"criterio 5: la fila conserva su error ({situ3})")
check(obse3 == "Could not send Message.", "criterio 5: y su motivo intacto")
check(os.listdir(m.DIR_ERRORES) and not os.listdir(m.DIR_PROCESADOS), "criterio 5: el zip fue a errores/")

# ---- criterio 3: los resumenes se siguen resolviendo igual ----
# Su numeracion no es serie-correlativo sino RC-YYYYMMDD-NNN, asi que la
# reconciliacion tiene que dejarla intacta en vez de intentar compararla.
nombre_rc = f"R{RUC}-{m._TIPO_RC}-RC-20260903-001.zip"
check(m._extraer_numeracion(nombre_rc) == "RC-20260903-001",
      "criterio 3: la numeracion del resumen sale del nombre")
check(m._reconciliar_numeracion("RC-20260903-001", nombre_rc) == "RC-20260903-001",
      "criterio 3: y la reconciliacion no la toca")

# ---- series distintas: el nombre nunca pisa una numeracion que no le corresponde ----
check(m._reconciliar_numeracion("B001-000009", f"R{RUC}-01-F003-009571.zip") == "B001-000009",
      "si las series no coinciden, se respeta la del XML")

if FALLAS:
    print(f"\n{FALLAS} FALLA(S)")
    sys.exit(1)
print("\nTODO OK")
