"""
Un CDR que quedo en 0 bytes no deja al comprobante colgado para siempre, y un
codigo de consulta fallida de SUNAT escala en vez de repetirse en silencio.

Corre sin base de datos ni SFS. Desde la raiz del proyecto:

    python pruebas/test_cdr_vacio.py
"""
import os
import sys
import tempfile
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as m                                            # noqa: E402
import logging                                              # noqa: E402
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp()
m.SFS_RPTA_DIR = os.path.join(TMP, "RPTA")
m.DIR_PROCESADOS = os.path.join(m.SFS_RPTA_DIR, "procesados")
m.DIR_ERRORES = os.path.join(m.SFS_RPTA_DIR, "errores")
for d in (m.SFS_RPTA_DIR, m.DIR_PROCESADOS, m.DIR_ERRORES):
    os.makedirs(d, exist_ok=True)
m._REINTENTOS_PATH = os.path.join(TMP, "reintentos.json")
m.conectar_bd = lambda: None

RUC = "20605858601"
FALLAS = 0


def check(cond, msg):
    global FALLAS
    if not cond:
        FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)


def poner(nombre, contenido=b"", edad_min=0):
    ruta = os.path.join(m.SFS_RPTA_DIR, nombre)
    with open(ruta, "wb") as fh:
        fh.write(contenido)
    if edad_min:
        viejo = time.time() - edad_min * 60
        os.utime(ruta, (viejo, viejo))
    return ruta


def limpiar():
    for d in (m.SFS_RPTA_DIR, m.DIR_PROCESADOS, m.DIR_ERRORES):
        for f in os.listdir(d):
            p = os.path.join(d, f)
            if os.path.isfile(p):
                os.remove(p)
    if os.path.exists(m._REINTENTOS_PATH):
        os.remove(m._REINTENTOS_PATH)


# --- 1. un ZIP recien creado y vacio se sigue esperando -----------------------
print("\n[1] archivo vacio reciente: se espera")
limpiar()
ruta = poner("R20605858601-01-F003-006240.zip", b"", edad_min=0)
check(not m._archivo_abandonado(ruta), "un vacio recien creado NO se da por abandonado")
m._barrer_rpta()
check(os.path.exists(ruta), "sigue en RPTA, esperando que termine de escribirse")
check(not os.listdir(m.DIR_ERRORES), "no se aparto a errores/")

# --- 2. el mismo ZIP, ya viejo, se aparta -------------------------------------
print("\n[2] archivo vacio viejo: se aparta")
limpiar()
ruta = poner("R20605858601-01-F003-006240.zip", b"", edad_min=m.MINUTOS_CDR_VACIO + 5)
check(m._archivo_abandonado(ruta), "un vacio viejo SI se da por abandonado")
m._barrer_rpta()
check(not os.path.exists(ruta), "ya no esta en RPTA")
check(os.listdir(m.DIR_ERRORES) == ["R20605858601-01-F003-006240.zip"],
      "quedo en errores/ para revisarlo")

# --- 3. un ZIP con contenido no se toca ---------------------------------------
print("\n[3] archivo con contenido")
limpiar()
ruta = poner("R20605858601-01-F003-000001.zip", b"PK\x03\x04 contenido", edad_min=120)
check(not m._archivo_abandonado(ruta), "con contenido nunca se da por abandonado, por viejo que sea")

# --- 4. _tiene_cdr no se deja enganar por un archivo vacio ---------------------
print("\n[4] _tiene_cdr")
limpiar()
poner("R20605858601-01-F003-006240.zip", b"")
check(not m._tiene_cdr(RUC, "01", "F003-006240"),
      "un CDR de 0 bytes NO cuenta como recuperado (si no, bloquea la reconsulta)")
limpiar()
poner("R20605858601-01-F003-006240.zip", b"PK\x03\x04")
check(m._tiene_cdr(RUC, "01", "F003-006240"), "uno con contenido si cuenta")

# --- 5. el 0125 se trata como consulta fallida, no como veredicto -------------
print("\n[5] codigo 0125")
limpiar()
RESP = {"valor": ("0125", "No se pudo obtener la constancia", None)}
m.consultar_estado_sunat = lambda ruc, tipo, num: RESP["valor"]

estado, cdr, _ = m.estado_en_sunat(RUC, "01", "F003-006240")
check(estado == "desconocido", f"no habilita el reenvio ({estado})")
check(cdr is None, "no inventa un CDR")
reg = json.load(open(m._REINTENTOS_PATH))
check("consulta:01-F003-006240" in reg, "queda contado como consulta fallida")
check(reg["consulta:01-F003-006240"]["codigo"] == "0125", "con su codigo guardado")

for _ in range(m.MAX_CONSULTAS_FALLIDAS - 1):
    m.estado_en_sunat(RUC, "01", "F003-006240")
veces = json.load(open(m._REINTENTOS_PATH))["consulta:01-F003-006240"]["consultas"]
check(veces == m.MAX_CONSULTAS_FALLIDAS,
      f"la cuenta llega al tope y ahi escala a revision manual ({veces})")


# --- 5b. un codigo conocido se informa distinto de uno que no lo es -----------
# La lista _CODIGOS_CONSULTA_FALLIDA no cambia el control de flujo --los dos casos
# devuelven 'desconocido' y los dos escalan al llegar al tope-- sino el mensaje: de
# un codigo conocido sabemos que conviene reconsultar, de uno desconocido no
# sabemos nada y eso amerita un WARNING. Sin esta comprobacion, vaciar la lista
# pasaba inadvertido.
print("\n[5b] el mensaje distingue lo conocido de lo que no")
logging.disable(logging.NOTSET)


class Captura(logging.Handler):
    def __init__(self):
        super().__init__()
        self.registros = []

    def emit(self, record):
        self.registros.append((record.levelno, record.getMessage()))


captura = Captura()
m.logger.addHandler(captura)
try:
    limpiar()
    RESP["valor"] = ("0125", "No se pudo obtener la constancia", None)
    m.estado_en_sunat(RUC, "01", "F003-000010")
    conocido = list(captura.registros)

    captura.registros.clear()
    limpiar()
    RESP["valor"] = ("0999", "codigo que nadie vio nunca", None)
    m.estado_en_sunat(RUC, "01", "F003-000011")
    desconocido = list(captura.registros)
finally:
    m.logger.removeHandler(captura)
    logging.disable(logging.CRITICAL)

check(conocido and max(n for n, _ in conocido) < logging.WARNING,
      "un 0125 se informa sin alarma: sabemos que hay que reconsultar")
check(any("consultar mas tarde" in t.lower() or "consultar más tarde" in t.lower()
          for _, t in conocido),
      "y el texto dice que se reintenta")
check(desconocido and max(n for n, _ in desconocido) >= logging.WARNING,
      "un codigo no reconocido si levanta WARNING")

# --- 6. cuando SUNAT responde algo concluyente, la racha se olvida ------------
print("\n[6] la racha se limpia")
RESP["valor"] = ("0127", "El ticket no existe", None)
estado, _, _ = m.estado_en_sunat(RUC, "01", "F003-006240")
check(estado == "no_registrado", f"el 0127 sigue habilitando el reenvio ({estado})")
check("consulta:01-F003-006240" not in json.load(open(m._REINTENTOS_PATH)),
      "y la racha de consultas fallidas se olvida")

limpiar()
RESP["valor"] = ("0", "aceptado", b"PK\x03\x04")
m.estado_en_sunat(RUC, "01", "F003-000002")
RESP["valor"] = ("0125", "No se pudo obtener la constancia", None)
m.estado_en_sunat(RUC, "01", "F003-000002")
RESP["valor"] = ("0", "aceptado", b"PK\x03\x04")
estado, _, _ = m.estado_en_sunat(RUC, "01", "F003-000002")
check(estado == "registrado", "un CDR recuperado despues de fallar se acepta igual")
check("consulta:01-F003-000002" not in json.load(open(m._REINTENTOS_PATH)),
      "y limpia su racha")

if FALLAS:
    print(str(FALLAS) + " FALLA(S)")
    sys.exit(1)
print("TODO OK")
