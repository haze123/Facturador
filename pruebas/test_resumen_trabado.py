"""
Un resumen que quedo en '05' por una consulta fallida se recupera por su ticket,
uno cuyo ticket ya no existe deja de reintentarse, y uno que retiene boletas sin
resolverse se ve en el log.

Corre sin base de datos ni SFS. Desde la raiz del proyecto:

    python pruebas/test_resumen_trabado.py
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
m.SFS_RPTA_DIR = os.path.join(TMP, "RPTA")
m.DIR_PROCESADOS = os.path.join(m.SFS_RPTA_DIR, "procesados")
os.makedirs(m.DIR_PROCESADOS, exist_ok=True)
m._REINTENTOS_PATH = os.path.join(TMP, "reintentos.json")
m._RESUMENES_PATH = os.path.join(TMP, "resumenes.json")
m.SOL_USUARIO, m.SOL_CLAVE = "FACTURA1", "clave"

RUC = "20605858601"
RC = "RC-20260906-098"
FALLAS = 0


def check(cond, msg):
    global FALLAS
    if not cond:
        FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)


def bd_sfs(filas):
    """filas: [(num_docu, ind_situ, num_ticket, des_obse)]"""
    ruta = os.path.join(TMP, "sfs%d.db" % time.time_ns())
    c = sqlite3.connect(ruta)
    c.execute("CREATE TABLE DOCUMENTO (NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, "
              "NOM_ARCH TEXT, IND_SITU TEXT, DES_OBSE TEXT, NUM_TICKET TEXT, FEC_ENVI TEXT)")
    c.executemany("INSERT INTO DOCUMENTO (NUM_RUC, TIP_DOCU, NUM_DOCU, NOM_ARCH, "
                  "IND_SITU, DES_OBSE, NUM_TICKET) VALUES (?,?,?,?,?,?,?)",
                  [(RUC, m._TIPO_RC, n, n, s, o, t) for n, s, t, o in filas])
    c.commit()
    c.close()
    m.SFS_BD_PATH = ruta
    return ruta


def resumenes(boletas, hace_horas=0):
    generado = time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(time.time() - hace_horas * 3600))
    with open(m._RESUMENES_PATH, "w", encoding="utf-8") as fh:
        json.dump({"resumenes": {RC: {"boletas": boletas, "generado": generado}}}, fh)


def limpiar():
    for p in (m._REINTENTOS_PATH, m._RESUMENES_PATH):
        if os.path.exists(p):
            os.remove(p)
    for d in (m.SFS_RPTA_DIR, m.DIR_PROCESADOS):
        for f in os.listdir(d):
            ruta = os.path.join(d, f)
            if os.path.isfile(ruta):
                os.remove(ruta)
    m._ultima_consulta.clear()


class Captura(logging.Handler):
    def __init__(self):
        super().__init__()
        self.registros = []

    def emit(self, record):
        self.registros.append((record.levelno, record.getMessage()))


def con_log(fn):
    """Corre fn capturando lo que loguea el daemon."""
    cap = Captura()
    logging.disable(logging.NOTSET)
    m.logger.addHandler(cap)
    try:
        fn()
    finally:
        m.logger.removeHandler(cap)
        logging.disable(logging.CRITICAL)
    return cap.registros


# --- 1. un RC en '05' con ticket valido vuelve a consultarse ------------------
print("\n[1] RC en '05' con ticket valido")
limpiar()
bd_sfs([(RC, "05", "TICKET-123", "Internal Error (from server)")])
check([n for n, _ in m._resumenes_con_ticket(RUC)] == [RC],
      "el RC en '05' entra en la lista de tickets a consultar")

GUARDADOS = []
m._guardar_cdr = lambda ruc, tip, num, cdr, msg: GUARDADOS.append(num)
m.consultar_ticket_sunat = lambda ruc, ticket: ("0", "El Resumen ha sido aceptado", b"PK\x03\x04")
m.recuperar_cdr_resumenes(RUC)
check(GUARDADOS == [RC], "se recupera su CDR y queda en RPTA para el hilo CDR")

# --- 2. un ticket ya consumido no se reintenta para siempre -------------------
print("\n[2] ticket que ya no existe")
limpiar()
bd_sfs([(RC, "05", "TICKET-123", "consultado antes")])
m.consultar_ticket_sunat = lambda ruc, ticket: (m._TICKET_NO_EXISTE, "El ticket no existe", None)
GUARDADOS.clear()
registros = con_log(lambda: m.recuperar_cdr_resumenes(RUC))
check(GUARDADOS == [], "no inventa un CDR")
check(any(n >= logging.ERROR for n, _ in registros), "avisa como ERROR, no en silencio")
check(not os.path.exists(m._REINTENTOS_PATH)
      or "consulta:RC-%s" % RC not in json.load(open(m._REINTENTOS_PATH)),
      "no acumula reintentos: es definitivo, no una falla pasajera")

# --- 3. una consulta que falla si acumula, y escala al tope -------------------
print("\n[3] consulta fallida: cuenta y escala")
limpiar()
bd_sfs([(RC, "05", "TICKET-123", "Internal Error (from server)")])
m.consultar_ticket_sunat = lambda ruc, ticket: (None, "Internal Error (from server)", None)
m.recuperar_cdr_resumenes(RUC)
reg = json.load(open(m._REINTENTOS_PATH))
clave = "consulta:%s-%s" % (m._TIPO_RC, RC)
check(clave in reg, "la consulta fallida queda contada")

for _ in range(m.MAX_CONSULTAS_FALLIDAS - 1):
    m._ultima_consulta.clear()
    m.recuperar_cdr_resumenes(RUC)
veces = json.load(open(m._REINTENTOS_PATH))[clave]["consultas"]
check(veces == m.MAX_CONSULTAS_FALLIDAS, "llega al tope (%d)" % veces)

m._ultima_consulta.clear()
registros = con_log(lambda: m.recuperar_cdr_resumenes(RUC))
check(any(n >= logging.ERROR for n, _ in registros),
      "pasado el tope se reporta como que requiere revision manual")

# --- 4. un RC trabado que retiene boletas se ve en el log ---------------------
print("\n[4] el RC trabado se reporta")
limpiar()
bd_sfs([(RC, "05", "TICKET-123", "Internal Error (from server)")])
resumenes(["B003-%06d" % i for i in range(1, 1240)], hace_horas=12)
registros = con_log(lambda: m._reportar_resumenes_trabados(RUC))
texto = " ".join(t for _, t in registros)
check(any(n >= logging.WARNING for n, _ in registros), "avisa al menos como WARNING")
check("1239" in texto, "dice cuantas boletas estan retenidas")
check(RC in texto, "nombra el resumen")
check("12." in texto or "11." in texto, "dice desde hace cuanto")

# --- 5. un RC recien generado no molesta, y uno cerrado tampoco ---------------
print("\n[5] sin ruido cuando no corresponde")
limpiar()
bd_sfs([(RC, "05", "TICKET-123", "recien salido")])
resumenes(["B003-000001"], hace_horas=0)
check(con_log(lambda: m._reportar_resumenes_trabados(RUC)) == [],
      "un RC recien generado no genera aviso")

limpiar()
bd_sfs([(RC, "03", "TICKET-123", "-")])
resumenes(["B003-000001"], hace_horas=12)
check(con_log(lambda: m._reportar_resumenes_trabados(RUC)) == [],
      "un RC ya cerrado tampoco")

if FALLAS:
    print(str(FALLAS) + " FALLA(S)")
    sys.exit(1)
print("TODO OK")
