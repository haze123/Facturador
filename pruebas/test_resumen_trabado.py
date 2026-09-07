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
from datetime import datetime, timedelta

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


def bd_sfs(filas, tipo=None):
    """filas: [(num_docu, ind_situ, num_ticket, des_obse)]; tipo por defecto, RC."""
    tipo = tipo or m._TIPO_RC
    ruta = os.path.join(TMP, "sfs%d.db" % time.time_ns())
    c = sqlite3.connect(ruta)
    c.execute("CREATE TABLE DOCUMENTO (NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, "
              "NOM_ARCH TEXT, IND_SITU TEXT, DES_OBSE TEXT, NUM_TICKET TEXT, FEC_ENVI TEXT)")
    c.executemany("INSERT INTO DOCUMENTO (NUM_RUC, TIP_DOCU, NUM_DOCU, NOM_ARCH, "
                  "IND_SITU, DES_OBSE, NUM_TICKET) VALUES (?,?,?,?,?,?,?)",
                  [(RUC, tipo, n, n, s, o, t) for n, s, t, o in filas])
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


# El logger del modulo escribe en el facturador.log real: sin esto, correr la suite
# ensuciaba el log de produccion con numeraciones inventadas.
m.logger.handlers = []
m.logger.propagate = False


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

# --- 6. un resumen rescatado desde '05' queda efectivamente cerrado -----------
# El rescate y el cierre son las dos puntas del mismo flujo, y se habian
# desincronizado: la consulta se amplio para levantar los '05' pero el cierre
# seguia exigiendo '08'/'09', asi que el UPDATE no encontraba la fila. El resumen
# se quedaba en '05' para siempre, reconsultandose y reportandose como trabado
# aunque sus boletas ya estuvieran en enviado=1.
print("\n[6] el resumen rescatado desde '05' se cierra")
for origen in ("05", "08", "09"):
    limpiar()
    ruta = bd_sfs([(RC, origen, "TICKET-123", "lo que sea")])
    m._cerrar_resumen_en_sfs(RUC, RC)
    c = sqlite3.connect(ruta)
    situ = c.execute("SELECT IND_SITU FROM DOCUMENTO WHERE NUM_DOCU=?", (RC,)).fetchone()[0]
    c.close()
    check(situ == "03", "desde '%s' queda cerrado en '03' (quedo en '%s')" % (origen, situ))

# Cerrado el resumen, las dos consecuencias que se veian en produccion se apagan.
limpiar()
bd_sfs([(RC, "03", "TICKET-123", "Aceptado (CDR procesado)")])
resumenes(["B003-%06d" % i for i in range(1, 1240)], hace_horas=12)
check(m._resumenes_con_ticket(RUC) == [], "deja de reconsultarse su ticket")
check(con_log(lambda: m._reportar_resumenes_trabados(RUC)) == [],
      "deja de reportarse como trabado")

# Un resumen que nunca salio no se puede dar por aceptado.
for origen in ("01", "02"):
    limpiar()
    ruta = bd_sfs([(RC, origen, "", "Enviado al SFS, esperando CDR")])
    m._cerrar_resumen_en_sfs(RUC, RC)
    c = sqlite3.connect(ruta)
    situ = c.execute("SELECT IND_SITU FROM DOCUMENTO WHERE NUM_DOCU=?", (RC,)).fetchone()[0]
    c.close()
    check(situ == origen, "un resumen en '%s' no se convierte en aceptado" % origen)

# --- 7. un codigo sin CDR cuenta contra el tope, no lo resetea ----------------
# El 0100 es un transitorio documentado de SUNAT. Antes reseteaba la racha en cada
# intento --se olvidaba apenas el codigo no fuera None-- y por eso nunca escalaba.
print("\n[7] un codigo sin CDR cuenta contra el tope")
clave = "consulta:%s-%s" % (m._TIPO_RC, RC)


def cuenta_actual():
    if not os.path.exists(m._REINTENTOS_PATH):
        return 0
    return (json.load(open(m._REINTENTOS_PATH)).get(clave) or {}).get("consultas", 0)


def consultar_n_veces(respuesta, veces):
    limpiar()
    bd_sfs([(RC, "05", "TICKET-123", "lo que sea")])
    m.consultar_ticket_sunat = lambda ruc, ticket: respuesta
    for _ in range(veces):
        m._ultima_consulta.clear()
        m.recuperar_cdr_resumenes(RUC)


consultar_n_veces(("0100", "El sistema no puede responder su solicitud", None),
                  m.MAX_CONSULTAS_FALLIDAS)
check(cuenta_actual() == m.MAX_CONSULTAS_FALLIDAS,
      "un fault 0100 acumula (%d)" % cuenta_actual())

m._ultima_consulta.clear()
registros = con_log(lambda: m.recuperar_cdr_resumenes(RUC))
check(any(n >= logging.ERROR for n, _ in registros),
      "pasado el tope escala a revision manual")

consultar_n_veces((m._TICKET_EN_PROCESO, "En proceso", None), 12)
check(cuenta_actual() == 0, "el 98 'en proceso' es concluyente: no acumula")

# Cuando por fin llega el CDR, la racha si tiene que olvidarse.
limpiar()
bd_sfs([(RC, "05", "TICKET-123", "lo que sea")])
m.consultar_ticket_sunat = lambda ruc, ticket: ("0100", "no responde", None)
m.recuperar_cdr_resumenes(RUC)
check(cuenta_actual() == 1, "arranca la racha")
GUARDADOS.clear()
m.consultar_ticket_sunat = lambda ruc, ticket: ("0", "aceptado", b"PK\x03\x04")
m._ultima_consulta.clear()
m.recuperar_cdr_resumenes(RUC)
check(GUARDADOS == [RC], "el CDR se recupera igual")
check(cuenta_actual() == 0, "y la racha se olvida al llegar el CDR")

# --- 8. un resumen con el CDR ya procesado se cierra ---------------------------
# _cerrar_resumen_en_sfs() solo corre al procesar un CDR, y recuperar_cdr_resumenes()
# saltea con `continue` todo resumen que ya tenga CDR en disco. Un resumen con el CDR
# archivado y la fila abierta quedaba entre esas dos: nadie lo reconsultaba, nadie lo
# reprocesaba, nadie lo cerraba. Permanente.
print("\n[8] resumen con el CDR ya procesado")


def poner_cdr(carpeta):
    ruta = os.path.join(carpeta, "R%s-%s-%s.zip" % (RUC, m._TIPO_RC, RC))
    with open(ruta, "wb") as fh:
        fh.write(b"PK\x03\x04")
    return ruta


def situ_de(ruta_bd):
    c = sqlite3.connect(ruta_bd)
    fila = c.execute("SELECT IND_SITU FROM DOCUMENTO WHERE NUM_DOCU=?", (RC,)).fetchone()
    c.close()
    return fila[0]


CONSULTADOS = []
m.consultar_ticket_sunat = lambda ruc, ticket: CONSULTADOS.append(ticket) or (None, "x", None)

limpiar()
ruta_bd = bd_sfs([(RC, "05", "TICKET-123", "Internal Error (from server)")])
poner_cdr(m.DIR_PROCESADOS)
CONSULTADOS.clear()
m.recuperar_cdr_resumenes(RUC)
check(situ_de(ruta_bd) == "03",
      "con el CDR en procesados/ queda cerrado (quedo en '%s')" % situ_de(ruta_bd))
check(CONSULTADOS == [], "y no vuelve a consultarle el ticket a SUNAT")

# Ya cerrado, se apagan los dos sintomas que se veian en produccion.
resumenes(["B003-%06d" % i for i in range(1, 1240)], hace_horas=12)
check(m._resumenes_con_ticket(RUC) == [], "deja de aparecer entre los que tienen ticket")
check(con_log(lambda: m._reportar_resumenes_trabados(RUC)) == [],
      "deja de reportarse como trabado")

# El CDR en RPTA todavia no se repartio entre las boletas: cerrar ahi daria el
# resumen por bueno con sus boletas en enviado=0.
limpiar()
ruta_bd = bd_sfs([(RC, "05", "TICKET-123", "Internal Error (from server)")])
poner_cdr(m.SFS_RPTA_DIR)
m._ultima_consulta.clear()
m.recuperar_cdr_resumenes(RUC)
check(situ_de(ruta_bd) == "05",
      "con el CDR aun en RPTA NO se cierra (quedo en '%s')" % situ_de(ruta_bd))

# --- 9. los codigos de SUNAT vienen en dos anchos distintos --------------------
# El exito vuelve como '0' y el "en proceso" como '0098', del mismo servicio. Con
# las constantes comparadas al texto crudo, '0098' == '98' daba False y la rama de
# "en proceso" era codigo muerto: la respuesta mas comun se contaba como falla.
print("\n[9] normalizacion del codigo del ticket")
check(m._norm_codigo_ticket("0098") == m._TICKET_EN_PROCESO, "'0098' es 'en proceso'")
check(m._norm_codigo_ticket("98") == m._TICKET_EN_PROCESO, "'98' tambien")
check(m._norm_codigo_ticket("0") in m._TICKET_CON_CDR, "'0' sigue siendo aceptado")
check(m._norm_codigo_ticket("0127") == m._TICKET_NO_EXISTE, "'0127' sigue siendo definitivo")
check(m._norm_codigo_ticket("") == "" and m._norm_codigo_ticket(None) == "",
      "vacio y None no se confunden con '0': significan que SUNAT no dijo nada")

limpiar()
bd_sfs([(RC, "08", "TICKET-123", "")])
m.consultar_ticket_sunat = lambda ruc, ticket: ("0098", "", None)
for _ in range(m.MAX_CONSULTAS_FALLIDAS + 2):
    m._ultima_consulta.clear()
    m.recuperar_cdr_resumenes(RUC)
reg = json.load(open(m._REINTENTOS_PATH)) if os.path.exists(m._REINTENTOS_PATH) else {}
check((reg.get("consulta:%s-%s" % (m._TIPO_RC, RC)) or {}).get("consultas", 0) == 0,
      "un '0098' repetido NO gasta el presupuesto de consultas fallidas")

# --- 10. un ticket eterno en "en proceso" escala ------------------------------
print("\n[10] el ticket en proceso tiene limite de tiempo")
limpiar()
bd_sfs([(RC, "08", "TICKET-123", "")])
resumenes(["B003-%06d" % i for i in range(1, 201)], hace_horas=24)
m.consultar_ticket_sunat = lambda ruc, ticket: ("0098", "", None)
registros = con_log(lambda: m.recuperar_cdr_resumenes(RUC))
check(all(n < logging.ERROR for n, _ in registros),
      "recien empezado no hace ruido")

# Se envejece la marca para no esperar horas reales.
datos = json.load(open(m._REINTENTOS_PATH))
viejo = datetime.now() - timedelta(hours=m.HORAS_TICKET_EN_PROCESO + 1)
datos["proceso:%s" % RC] = {"desde": viejo.strftime("%Y-%m-%d %H:%M:%S")}
with open(m._REINTENTOS_PATH, "w", encoding="utf-8") as fh:
    json.dump(datos, fh)

m._ultima_consulta.clear()
registros = con_log(lambda: m.recuperar_cdr_resumenes(RUC))
texto = " ".join(t for _, t in registros)
check(any(n >= logging.ERROR for n, _ in registros), "pasado el umbral escala a ERROR")
check("200" in texto, "dice cuantas boletas retiene")
check("NO se reenvia" in texto or "NO se reenvía" in texto,
      "y deja explicito que no se reenvia solo")

# Cuando por fin llega el CDR, la cuenta de horas se olvida.
GUARDADOS.clear()
m.consultar_ticket_sunat = lambda ruc, ticket: ("0", "aceptado", b"PK\x03\x04")
m._ultima_consulta.clear()
m.recuperar_cdr_resumenes(RUC)
check(GUARDADOS == [RC], "el CDR se recupera")
check("proceso:%s" % RC not in json.load(open(m._REINTENTOS_PATH)),
      "y la cuenta de horas se limpia")

# --- 11. un resumen en '06' no se consulta como si fuera una factura ----------
# estado_en_sunat() va contra billConsultService, que no acepta tipo RC: responde
# 0009 y el resumen quedaba reintentando una consulta imposible para siempre.
print("\n[11] resumen en '06': el ticket decide, no estado_en_sunat")


def explota(*a, **k):
    raise AssertionError("estado_en_sunat no debe llamarse para un resumen")


MARCADOS = []


class FakeBD:
    @staticmethod
    def marcar_enviado(conn, num, enviado=True, limpiar_error=True):
        MARCADOS.append(num)
        return 1


m._bd = lambda: FakeBD()
m._escribir_bd = lambda fn, conn, *a, **k: fn(conn, *a, **k)
m.estado_en_sunat = explota
RED = "Hubo un problema al invocar servicio SUNAT: Could not send Message."

# Sin ticket: SUNAT no lo recibio, se puede volver a armar.
limpiar()
MARCADOS.clear()
ruta_bd = bd_sfs([(RC, "06", "", RED)])
resumenes(["B003-000001"])
m.resetear_rechazados(None, RUC)
# Un RC vuelve a la cola descartandose --sus boletas se reagrupan en uno nuevo--, no
# con marcar_enviado(), que con la numeracion de un resumen no matchea ninguna fila.
check(RC not in (json.load(open(m._RESUMENES_PATH)).get("resumenes") or {}),
      "sin ticket se descarta para volver a armarse")
c = sqlite3.connect(ruta_bd)
quedan = c.execute("SELECT COUNT(*) FROM DOCUMENTO WHERE NUM_DOCU=?", (RC,)).fetchone()[0]
c.close()
check(quedan == 0, "y se saca de la bandeja para regenerarse")

# Con ticket: SUNAT ya lo recibio, reenviarlo duplicaria las boletas.
limpiar()
MARCADOS.clear()
ruta_bd = bd_sfs([(RC, "06", "TICKET-123", RED)])
m.resetear_rechazados(None, RUC)
check(MARCADOS == [], "con ticket NO se reenvia")
c = sqlite3.connect(ruta_bd)
quedan = c.execute("SELECT COUNT(*) FROM DOCUMENTO WHERE NUM_DOCU=?", (RC,)).fetchone()[0]
c.close()
check(quedan == 1, "y sigue en la bandeja")

# Una factura conserva el camino de siempre: ahi estado_en_sunat SI corresponde.
limpiar()
MARCADOS.clear()
bd_sfs([("F003-000123", "06", "", RED)], tipo="01")
llamadas = []
m.estado_en_sunat = lambda ruc, tip, num: llamadas.append((tip, num)) or ("no_registrado", None, "")
m.resetear_rechazados(None, RUC)
check(llamadas == [("01", "F003-000123")],
      "una factura si se consulta con estado_en_sunat (%s)" % llamadas)
check(MARCADOS == ["F003-000123"], "y vuelve a la cola como antes")

# --- 12. reencolar un resumen tiene que liberar sus boletas de verdad ----------
# Reencolarlo borra su fila de la bandeja, y sin rastro _boletas_en_resumenes_activos()
# retiene ante la duda --correctamente, porque ahi no sabe que paso--. Si al descartar
# el resumen no se olvida tambien su entrada en resumenes.json, sus boletas quedan
# retenidas por algo que ya no existe: un bloqueo cambiado por otro.
print("\n[12] las boletas se liberan al descartar el resumen")
BOLETAS_RC = ["B003-%06d" % i for i in range(1, 201)]

limpiar()
MARCADOS.clear()
m.estado_en_sunat = explota
bd_sfs([(RC, "06", "", RED)])
resumenes(BOLETAS_RC)
registros = con_log(lambda: m.resetear_rechazados(None, RUC))
check(m._boletas_en_resumenes_activos(RUC) == set(),
      "sin ticket, las 200 boletas quedan libres para un resumen nuevo")
check(RC not in (json.load(open(m._RESUMENES_PATH)).get("resumenes") or {}),
      "y el resumen descartado sale de resumenes.json")
texto = " ".join(t for _, t in registros)
check("200" in texto, "el log dice cuantas boletas vuelven a la cola")
check(MARCADOS == [],
      "no se llama marcar_enviado con la numeracion del RC: no es fila de Comprobantes")

# Con ticket no se descarta nada: SUNAT lo recibio y sus boletas siguen ligadas.
limpiar()
bd_sfs([(RC, "06", "TICKET-123", RED)])
resumenes(BOLETAS_RC)
m.resetear_rechazados(None, RUC)
check(len(m._boletas_en_resumenes_activos(RUC)) == 200,
      "con ticket las boletas siguen retenidas")
check(RC in (json.load(open(m._RESUMENES_PATH)).get("resumenes") or {}),
      "y el resumen sigue registrado")

if FALLAS:
    print(str(FALLAS) + " FALLA(S)")
    sys.exit(1)
print("TODO OK")
