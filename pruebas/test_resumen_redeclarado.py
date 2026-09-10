"""
Descartar un resumen no puede destruir el registro de que boletas llevaba.

El 2026-09-09, tras un bloqueo de SUNAT de ~19 horas, el daemon armo 84 resumenes en
un dia --lo normal son 2-- y declaro 143 boletas 40 veces cada una. El envio fallaba
sin devolver ticket, se descartaba el resumen dando por hecho que SUNAT no lo habia
recibido, y sus boletas volvian a la cola; pero varios SI habian llegado y quedaron
encolados del lado de SUNAT, que los acepto al recuperarse. Ese CDR tardio llegaba sin
mapeo, no cerraba nada, y el ciclo reagrupaba las mismas boletas otra vez.

Cubre las dos mitades del arreglo: conservar el mapeo (y poder honrar un CDR tardio) y
frenar antes de que el bucle se vuelva decenas de declaraciones. Mas la poda, que es lo
que evita que conservar el mapeo haga crecer resumenes.json sin fin.

Corre sin base de datos ni SFS. Desde la raiz del proyecto:

    python pruebas/test_resumen_redeclarado.py
"""
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as m                                            # noqa: E402
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp()
m._RESUMENES_PATH = os.path.join(TMP, "resumenes.json")
m._REINTENTOS_PATH = os.path.join(TMP, "reintentos.json")
m._SFS_FIRMA_DIR = os.path.join(TMP, "FIRMA")
m.SFS_RPTA_DIR = os.path.join(TMP, "RPTA")
m.DIR_PROCESADOS = os.path.join(m.SFS_RPTA_DIR, "procesados")
os.makedirs(m._SFS_FIRMA_DIR, exist_ok=True)
os.makedirs(m.DIR_PROCESADOS, exist_ok=True)
m.EMISOR_RUC_OVERRIDE = RUC = "20609785269"
m._cerrar_resumen_en_sfs = lambda ruc, num, veredicto="": None

MARCADOS = []
PENDIENTES = []


class FakeBD:
    @staticmethod
    def marcar_enviados(conn, nums, limpiar_error=True):
        MARCADOS.extend(nums)
        return len(nums)

    @staticmethod
    def guardar_error(conn, num, detalle):
        return 1

    @staticmethod
    def pendientes(conn):
        return [{"numeracion_comprobante": n} for n in PENDIENTES]


m._bd = lambda: FakeBD()
m._escribir_bd = lambda fn, conn, *a, **k: fn(conn, *a, **k)


class Captura(logging.Handler):
    def __init__(self):
        super().__init__()
        self.registros = []

    def emit(self, record):
        self.registros.append((record.levelno, record.getMessage()))


# El logger del modulo escribe en el facturador.log real: sin esto, correr la suite
# ensuciaba el log de produccion con numeraciones inventadas.
m.logger.handlers = []
m.logger.propagate = False


def con_log(fn):
    cap = Captura()
    logging.disable(logging.NOTSET)
    m.logger.addHandler(cap)
    try:
        resultado = fn()
    finally:
        m.logger.removeHandler(cap)
        logging.disable(logging.CRITICAL)
    return resultado, cap.registros


def limpiar():
    MARCADOS.clear()
    PENDIENTES.clear()
    for p in (m._RESUMENES_PATH, m._REINTENTOS_PATH):
        if os.path.exists(p):
            os.remove(p)


def leer():
    return json.load(open(m._RESUMENES_PATH, encoding="utf-8")).get("resumenes") or {}


def escribir(resumenes, extra=None):
    datos = {"resumenes": resumenes}
    datos.update(extra or {})
    m._guardar_resumenes(datos)


def hace(dias, horas=0):
    return (datetime.now() - timedelta(days=dias, hours=horas)).strftime("%Y-%m-%d %H:%M:%S")


FALLAS = 0


def check(cond, msg):
    global FALLAS
    if not cond:
        FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)
    return cond


ACEPTADO = {"status": "ACEPTADO", "lineas": []}
BOLETAS = [f"B003-{i:06d}" for i in range(1, 201)]


# --- 1. descartar conserva el mapeo y libera las boletas ----------------------
print("\n[1] descartar conserva el mapeo")
limpiar()
m._registrar_resumen("RC-20260909-001", BOLETAS)
libres = m._olvidar_resumen("RC-20260909-001")
check(libres == BOLETAS, "devuelve las 200 boletas para reagruparlas")
entrada = leer().get("RC-20260909-001") or {}
check(bool(entrada.get("descartado")), "la entrada sigue, marcada como descartada")
check(entrada.get("boletas") == BOLETAS, "y conserva las 200 boletas que llevaba")
check(m._boletas_de_resumen("RC-20260909-001") == BOLETAS,
      "_boletas_de_resumen() la sigue encontrando")

m._docs_en_vuelo = lambda ruc: {}
check(m._boletas_en_resumenes_activos(RUC) == set(),
      "_boletas_en_resumenes_activos() no retiene por un resumen descartado")


# --- 2. el CDR tardio de un resumen descartado cierra sus boletas -------------
# Es el nudo del incidente: sin mapeo esto fallaba con "no hay boletas registradas",
# las boletas seguian en enviado=0 y el ciclo las reagrupaba una y otra vez.
print("\n[2] CDR tardio de un resumen descartado")
limpiar()
m._registrar_resumen("RC-20260909-002", BOLETAS)
m._olvidar_resumen("RC-20260909-002")
ok, registros = con_log(lambda: m._actualizar_sql_cdr(None, "RC-20260909-002", ACEPTADO))
check(ok is True, "el CDR se procesa (devuelve True)")
check(set(MARCADOS) == set(BOLETAS), "las 200 boletas quedan enviado=1")
check(any("se había descartado" in t for _, t in registros),
      "el log deja constancia de que se lo habia dado por perdido")
check(bool((leer().get("RC-20260909-002") or {}).get("cerrado")),
      "y la entrada queda marcada como cerrada")
check(not (leer().get("RC-20260909-002") or {}).get("descartado"),
      "ya no figura descartada: su CDR llego")


# --- 3. el mismo caso, pero las boletas ya viajaron en otro resumen -----------
# Aca hay un duplicado real ante SUNAT, y solo se deshace con una comunicacion de
# baja: tiene que verse en el log, no pasar en silencio.
print("\n[3] CDR tardio cuando las boletas ya se redeclararon")
limpiar()
m._registrar_resumen("RC-20260909-003", BOLETAS)
m._olvidar_resumen("RC-20260909-003")
m._registrar_resumen("RC-20260909-004", BOLETAS)      # se reagruparon en uno nuevo
ok, registros = con_log(lambda: m._actualizar_sql_cdr(None, "RC-20260909-003", ACEPTADO))
check(ok is True, "no rompe: el CDR se procesa igual")
check(set(MARCADOS) == set(BOLETAS), "las boletas se cierran igual")
check(any("duplicado ante SUNAT" in t and "RC-20260909-004" in t for _, t in registros),
      "el log nombra el resumen con el que hay duplicado")
check(any(nivel >= logging.ERROR for nivel, t in registros if "duplicado" in t),
      "y lo hace con nivel ERROR, no escondido en un INFO")


# --- 4. un resumen aceptado normal sigue igual (sin regresion) ----------------
print("\n[4] el camino normal no cambia")
limpiar()
m._registrar_resumen("RC-20260909-005", BOLETAS)
ok, registros = con_log(lambda: m._actualizar_sql_cdr(None, "RC-20260909-005", ACEPTADO))
check(ok is True, "devuelve True")
check(set(MARCADOS) == set(BOLETAS), "las 200 se marcan enviado=1")
check(not any("se había descartado" in t for _, t in registros),
      "sin el aviso de descartado, que aca no corresponde")


# --- 5. reconstruir el mapeo desde el XML firmado -----------------------------
# Las entradas que borro la version anterior de _olvidar_resumen() ya no estan. Su
# CDR tardio tiene que poder cerrar sus boletas igual, y el XML de FIRMA/ es lo que
# efectivamente se le declaro a SUNAT.
print("\n[5] respaldo: reconstruir desde FIRMA/")
limpiar()
xml = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<SummaryDocuments xmlns="urn:sunat" xmlns:cbc="urn:cbc">'
    '<cbc:ID>RC-20260909-006</cbc:ID>'
    + "".join(f'<Line><cbc:ID>{b}</cbc:ID></Line>' for b in BOLETAS[:5])
    + '</SummaryDocuments>'
)
firma = os.path.join(m._SFS_FIRMA_DIR, m._nombre_archivo_rc(RUC, "RC-20260909-006") + ".xml")
open(firma, "w", encoding="utf-8").write(xml)
check(m._boletas_desde_firma(RUC, "RC-20260909-006") == BOLETAS[:5],
      "saca las 5 boletas del XML firmado")
check("RC-20260909-006" not in m._boletas_desde_firma(RUC, "RC-20260909-006"),
      "y no confunde el id del propio resumen con una boleta")

ok, registros = con_log(lambda: m._actualizar_sql_cdr(None, "RC-20260909-006", ACEPTADO))
check(ok is True, "un CDR sin entrada en resumenes.json igual se procesa")
check(set(MARCADOS) == set(BOLETAS[:5]), "y cierra las boletas reconstruidas")
check(any("FIRMA/" in t for _, t in registros), "el log dice de donde salio el mapeo")


# --- 6. el freno por boletas redeclaradas -------------------------------------
print("\n[6] freno: el mismo lote girando en falso")
limpiar()
hoy = datetime.now().strftime("%Y%m%d")
check(m._motivo_para_frenar(BOLETAS, hoy) == "", "con el archivo vacio no frena nada")
for i in range(m.MAX_DECLARACIONES_BOLETA):
    m._registrar_resumen(f"RC-{hoy}-{i+1:03d}", BOLETAS)
    m._olvidar_resumen(f"RC-{hoy}-{i+1:03d}")
motivo = m._motivo_para_frenar(BOLETAS, hoy)
check(motivo != "", f"tras {m.MAX_DECLARACIONES_BOLETA} declaraciones frena ({motivo})")
check("200 boleta" in motivo, "y dice cuantas boletas son")


# --- 7. el freno por tope diario ----------------------------------------------
# Red de seguridad por si el bucle vuelve de una forma que el contador por boleta no
# atrape: 84 resumenes en un dia no puede pasar en silencio.
print("\n[7] freno: tope de resumenes por dia")
limpiar()
for i in range(m.MAX_RESUMENES_DIA):
    m._registrar_resumen(f"RC-{hoy}-{i+1:03d}", [f"B003-{i:06d}"])
motivo = m._motivo_para_frenar(["B003-999999"], hoy)
check(motivo != "", f"al llegar al tope diario frena ({motivo})")
check(str(m.MAX_RESUMENES_DIA) in motivo, "y dice cual es el tope")
check(m._motivo_para_frenar(["B003-999999"], "20260101") == "",
      "el tope es por dia: otro dia arranca limpio")


# --- 8. la poda -------------------------------------------------------------
print("\n[8] poda de resumenes.json")
limpiar()
escribir({
    "RC-20260101-001": {"boletas": ["B003-000001"], "generado": hace(60),
                        "cerrado": hace(60)},
    "RC-20260101-002": {"boletas": ["B003-000002"], "generado": hace(60),
                        "descartado": hace(60)},
    "RC-20260101-003": {"boletas": ["B003-000003"], "generado": hace(60)},
    "RC-20260101-004": {"boletas": ["B003-000004"], "generado": hace(60),
                        "cerrado": hace(60)},
    "RC-20260909-009": {"boletas": ["B003-000009"], "generado": hace(0),
                        "cerrado": hace(0)},
})
PENDIENTES.append("B003-000004")          # esta quedo afuera por codigo de linea
_, registros = con_log(lambda: m._podar_resumenes(None))
quedan = leer()
check("RC-20260101-001" not in quedan, "poda una cerrada y vieja")
check("RC-20260101-002" not in quedan, "poda una descartada y vieja")
check("RC-20260101-003" in quedan,
      "NO poda una sin resolver, por vieja que sea: ahi la antiguedad es la senal")
check("RC-20260101-004" in quedan,
      "NO poda una cerrada cuyas boletas siguen en enviado=0")
check("RC-20260909-009" in quedan, "NO poda una reciente")
check(any("enviado=0" in t for _, t in registros), "avisa cual conservo y por que")

# Corre una sola vez al dia: es mantenimiento, y su consulta de pendientes no tiene
# por que repetirse en cada ciclo.
escribir({"RC-20260101-005": {"boletas": ["B003-000005"], "generado": hace(60),
                              "cerrado": hace(60)}},
         extra={"ultima_poda": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
m._podar_resumenes(None)
check("RC-20260101-005" in leer(), "no vuelve a podar si ya se podo hoy")


print()
if FALLAS:
    print(f"{FALLAS} FALLA(S)")
    sys.exit(1)
print("TODO OK")
