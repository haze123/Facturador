"""
El corte SAAJ se reconoce como falla de red, y un resumen en error nunca queda muerto.

Produccion, 2026-09-10: el SFS dejo 6 resumenes con

    Nro. Ticket: Problem writing SAAJ model to stream: e-factura.sunat.gob.pe

Es la capa SOAP de Java: no pudo ni escribir la solicitud, el envio nunca salio. Como
la frase no estaba en _SENALES_DE_RED, esos RC cayeron por el camino del rechazo real,
que para un resumen era un pozo: marcar_enviado() con una numeracion RC no matchea
ninguna fila, el DELETE lo sacaba de la bandeja, y nadie llamaba a _olvidar_resumen().
El resumen desaparecia de todos lados menos de resumenes.json, reteniendo sus 1195
boletas para siempre.

Se prueban las dos mitades por separado, y eso importa: la lista de senales es corta a
proposito, asi que el camino del rechazo va a seguir recibiendo mensajes desconocidos.
Que ahi un RC no quede muerto es lo que evita que el proximo mensaje nuevo repita el
incidente.

Corre sin base de datos ni SFS. Desde la raiz del proyecto:

    python pruebas/test_corte_saaj.py
"""
import json
import logging
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as m                                            # noqa: E402
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp()
m._REINTENTOS_PATH = os.path.join(TMP, "reintentos.json")
m._RESUMENES_PATH = os.path.join(TMP, "resumenes.json")
m.SFS_DATA_DIR = os.path.join(TMP, "DATA")
os.makedirs(m.SFS_DATA_DIR, exist_ok=True)
m.EMISOR_RUC_OVERRIDE = RUC = "20612016527"

SAAJ = "Nro. Ticket: Problem writing SAAJ model to stream: e-factura.sunat.gob.pe"
LEER = "Problem reading SAAJ model from stream: e-factura.sunat.gob.pe"
RED = "Hubo un problema al invocar servicio SUNAT: Could not send Message."
PERFIL = "0111 - No tiene el perfil para enviar comprobantes electronicos"
DATO = "2335 - El XML no cumple con el formato esperado"

BOLETAS = ["B003-%06d" % i for i in range(1, 1196)]
MARCADOS = []


class FakeBD:
    @staticmethod
    def marcar_enviado(conn, num, enviado=True, limpiar_error=True):
        MARCADOS.append(num)
        return 0                 # un RC no matchea ninguna fila de Comprobantes


m._bd = lambda: FakeBD()
m._escribir_bd = lambda fn, conn, *a, **k: fn(conn, *a, **k)
m._docs_en_vuelo = lambda ruc: {}


class Captura(logging.Handler):
    def __init__(self):
        super().__init__()
        self.registros = []

    def emit(self, record):
        self.registros.append(record.getMessage())


m.logger.handlers = []
m.logger.propagate = False


def con_log(fn):
    cap = Captura()
    logging.disable(logging.NOTSET)
    m.logger.addHandler(cap)
    try:
        fn()
    finally:
        m.logger.removeHandler(cap)
        logging.disable(logging.CRITICAL)
    return cap.registros


def bd(num_docu, situ, obse, ticket="", tipo=None):
    ruta = os.path.join(TMP, "s%d.db" % time.time_ns())
    c = sqlite3.connect(ruta)
    c.execute("CREATE TABLE DOCUMENTO (NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, "
              "NOM_ARCH TEXT, IND_SITU TEXT, DES_OBSE TEXT, NUM_TICKET TEXT, FEC_ENVI TEXT)")
    c.execute("INSERT INTO DOCUMENTO (NUM_RUC, TIP_DOCU, NUM_DOCU, NOM_ARCH, IND_SITU, "
              "DES_OBSE, NUM_TICKET) VALUES (?,?,?,?,?,?,?)",
              (RUC, tipo or m._TIPO_RC, num_docu, num_docu, situ, obse, ticket))
    c.commit()
    c.close()
    m.SFS_BD_PATH = ruta
    return ruta


def sigue_en_bandeja(ruta, num_docu):
    c = sqlite3.connect(ruta)
    n = c.execute("SELECT COUNT(*) FROM DOCUMENTO WHERE NUM_DOCU=?", (num_docu,)).fetchone()[0]
    c.close()
    return n > 0


def entrada(num_docu):
    return ((json.load(open(m._RESUMENES_PATH, encoding="utf-8")).get("resumenes") or {})
            .get(num_docu) or {})


def data_files(num_docu):
    """Deja el .RDI/.TRD como los escribe generar_resumen_diario()."""
    base = m._nombre_archivo_rc(RUC, num_docu)
    for ext in ("RDI", "TRD"):
        open(os.path.join(m.SFS_DATA_DIR, "%s.%s" % (base, ext)), "w").write("x")
    return base


def en_data(base, ext):
    return os.path.exists(os.path.join(m.SFS_DATA_DIR, "%s.%s" % (base, ext)))


def limpiar():
    MARCADOS.clear()
    for p in (m._REINTENTOS_PATH, m._RESUMENES_PATH):
        if os.path.exists(p):
            os.remove(p)
    for f in os.listdir(m.SFS_DATA_DIR):
        os.remove(os.path.join(m.SFS_DATA_DIR, f))


FALLAS = 0


def check(cond, msg):
    global FALLAS
    if not cond:
        FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)


# --- 1. la frase del incidente se reconoce como corte de red -----------------
print("\n[1] _es_falla_de_red reconoce el corte SAAJ")
check(m._es_falla_de_red(SAAJ), "el mensaje SAAJ del incidente es falla de red")
check(m._es_falla_de_red(RED), "el corte ya conocido sigue siendolo")

# El criterio de la lista es "corto y literal": un rechazo real de SUNAT NO puede
# entrar, porque reintentarlo no lo arregla y quedaria reencolado para siempre.
check(not m._es_falla_de_red(PERFIL), "el 0111 (perfil) NO es falla de red")
check(not m._es_falla_de_red(DATO), "un rechazo por formato NO es falla de red")
check(not m._es_falla_de_red(""), "un motivo vacio tampoco")

# "writing" y no "saaj model" a secas: el de lectura es el caso opuesto --la
# solicitud SI salio-- y tratarlo como corte autorizaria a reenviar un resumen que
# SUNAT quiza ya tiene.
check(not m._es_falla_de_red(LEER),
      "el SAAJ de LECTURA no se confunde con el de escritura")


# --- 2. el RC del incidente ya no retiene sus boletas ------------------------
print("\n[2] el resumen del incidente libera sus 1195 boletas")
limpiar()
m._registrar_resumen("RC-20260910-125", BOLETAS)
base = data_files("RC-20260910-125")
ruta = bd("RC-20260910-125", "06", SAAJ)
m.resetear_rechazados(None, RUC)
check(m._boletas_en_resumenes_activos(RUC) == set(),
      "las 1195 boletas quedan libres para un resumen nuevo")
check(bool(entrada("RC-20260910-125").get("descartado")),
      "el resumen queda marcado descartado, conservando su mapeo")
check(MARCADOS == [], "no se llama a marcar_enviado() con una numeracion RC")

# Sin esto queda una carrera: el SFS envia desde los archivos de DATA, no desde la
# tabla, y sincronizar_bandeja_sfs() lo obliga a releer esa carpeta en cada ciclo. El
# .RDI huerfano volvia a registrarse con la numeracion original y salia otra vez,
# mientras el daemon armaba uno nuevo con las mismas boletas. Produccion 2026-09-10.
check(not en_data(base, "RDI"), "el .RDI sale de DATA: el SFS ya no puede reenviarlo")
check(not en_data(base, "TRD"), "y el .TRD tambien")


# --- 3. lo mismo con un motivo que NO reconocemos ----------------------------
# Es la mitad que importa a futuro: la lista es corta a proposito, asi que el camino
# del rechazo va a seguir recibiendo mensajes nuevos. Ahi un RC tampoco puede morir.
print("\n[3] un motivo desconocido tampoco deja el resumen muerto")
limpiar()
m._registrar_resumen("RC-20260910-126", BOLETAS)
base = data_files("RC-20260910-126")
ruta = bd("RC-20260910-126", "06", "Algo que todavia no sabemos leer")
registros = con_log(lambda: m.resetear_rechazados(None, RUC))
check(m._boletas_en_resumenes_activos(RUC) == set(),
      "sus boletas vuelven a la cola igual")
check(bool(entrada("RC-20260910-126").get("descartado")), "y el mapeo se conserva")
check(not sigue_en_bandeja(ruta, "RC-20260910-126"), "la fila sale de la bandeja")
check(any("sin llegar a obtener ticket" in r for r in registros),
      "el log explica por que se descarto")
check(any("Algo que todavia no sabemos leer" in r for r in registros),
      "y deja a la vista el motivo que no supimos clasificar")
check(MARCADOS == [], "sin marcar_enviado() inutil")
check(not en_data(base, "RDI"), "sus archivos tambien salen de DATA")


# --- 4. un resumen CON ticket no se toca -------------------------------------
# SUNAT ya lo recibio: su CDR llega detras del ticket, que se lee de esta misma fila.
# Borrarla lo dejaba sin nada que consultar, y descartarlo redeclararia sus boletas.
print("\n[4] un resumen con ticket se deja para recuperar_cdr_resumenes()")
limpiar()
m._registrar_resumen("RC-20260910-127", BOLETAS)
base = data_files("RC-20260910-127")
ruta = bd("RC-20260910-127", "06", "Algo que todavia no sabemos leer", ticket="123456")
m.resetear_rechazados(None, RUC)
check(sigue_en_bandeja(ruta, "RC-20260910-127"),
      "la fila SIGUE en la bandeja: ahi vive el ticket que hay que consultar")
check(not entrada("RC-20260910-127").get("descartado"),
      "no se descarta: SUNAT ya lo tiene")
check(m._boletas_en_resumenes_activos(RUC) == set(BOLETAS),
      "y sus boletas siguen retenidas, que es lo correcto")
# Los archivos NO se tocan: solo se borran al descartar. Borrarlos de un resumen que
# SUNAT ya recibio le sacaria al SFS lo unico con que puede terminar de procesarlo.
check(en_data(base, "RDI"), "y su .RDI se conserva: no se descarto nada")


# --- 5. una factura sigue por su camino de siempre (sin regresion) -----------
print("\n[5] un comprobante suelto no cambia")
limpiar()
ruta = bd("F003-006466", "10", PERFIL, tipo="01")
m.resetear_rechazados(None, RUC)
check(MARCADOS == ["F003-006466"],
      "una factura rechazada sigue volviendo a la cola con marcar_enviado()")
check(m._reintentos_de("F003-006466") == 1, "y gastando su reintento")


print()
if FALLAS:
    print(f"{FALLAS} FALLA(S)")
    sys.exit(1)
print("TODO OK")
