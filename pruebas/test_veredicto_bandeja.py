"""
DES_OBSE de la bandeja del SFS tiene que decir lo que SUNAT contesto, no "Aceptado".

El 2026-09-09 quedaron 40 resumenes rotulados 'Aceptado (CDR procesado)' de los cuales
39 SUNAT los habia rechazado --34 con el codigo 2282, "Existe documento ya informado
anteriormente"--. El texto era fijo: se escribia igual para un aceptado que para un
rechazo.

No hubo dano funcional --las boletas de los rechazados siguieron en enviado=0, que es
lo correcto-- pero si de diagnostico: ese texto llevo a concluir que 143 boletas se
habian declarado 40 veces y que hacia falta una comunicacion de baja ante SUNAT, cuando
SUNAT habia rechazado los repetidos y no habia ningun duplicado.

Corre sin base de datos ni SFS. Desde la raiz del proyecto:

    python pruebas/test_veredicto_bandeja.py
"""
import logging
import os
import sqlite3
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as m                                            # noqa: E402
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp()
m.SFS_RPTA_DIR = os.path.join(TMP, "RPTA")
m.DIR_PROCESADOS = os.path.join(m.SFS_RPTA_DIR, "procesados")
os.makedirs(m.DIR_PROCESADOS, exist_ok=True)
m._RESUMENES_PATH = os.path.join(TMP, "resumenes.json")
m._REINTENTOS_PATH = os.path.join(TMP, "reintentos.json")
m.EMISOR_RUC_OVERRIDE = RUC = "20609785269"
m.logger.handlers = []
m.logger.propagate = False

RC = "RC-20260909-001"
BOLETAS = ["B003-000001", "B003-000002"]


class FakeBD:
    @staticmethod
    def guardar_error_varios(conn, nums, detalle):
        return len(nums)

    @staticmethod
    def marcar_enviados(conn, nums, limpiar_error=True):
        return len(nums)


m._bd = lambda: FakeBD()
m._escribir_bd = lambda fn, conn, *a, **k: fn(conn, *a, **k)

FALLAS = 0


def check(cond, msg):
    global FALLAS
    if not cond:
        FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)
    return cond


_N = [0]


def bd_sfs(num_docu, situ="09", tipo=None):
    _N[0] += 1                      # una BD por caso: reusar el archivo la duplicaria
    ruta = os.path.join(TMP, "sfs_%d.db" % _N[0])
    c = sqlite3.connect(ruta)
    c.execute("CREATE TABLE DOCUMENTO (NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, "
              "NOM_ARCH TEXT, IND_SITU TEXT, DES_OBSE TEXT, NUM_TICKET TEXT, FEC_ENVI TEXT)")
    c.execute("INSERT INTO DOCUMENTO (NUM_RUC, TIP_DOCU, NUM_DOCU, NOM_ARCH, IND_SITU) "
              "VALUES (?,?,?,?,?)", (RUC, tipo or m._TIPO_RC, num_docu, num_docu, situ))
    c.commit()
    c.close()
    m.SFS_BD_PATH = ruta
    return ruta


def obse(ruta, num_docu):
    c = sqlite3.connect(ruta)
    fila = c.execute("SELECT IND_SITU, DES_OBSE FROM DOCUMENTO WHERE NUM_DOCU=?",
                     (num_docu,)).fetchone()
    c.close()
    return fila


def cdr_zip(carpeta, tip, num, codigo, descripcion):
    """Deja en disco un CDR como el que archiva el daemon."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<ApplicationResponse xmlns="urn:ar" xmlns:cac="urn:cac" xmlns:cbc="urn:cbc">'
        f'<cbc:ID>{num}</cbc:ID>'
        '<cac:DocumentResponse><cac:Response>'
        f'<cbc:ResponseCode>{codigo}</cbc:ResponseCode>'
        f'<cbc:Description>{descripcion}</cbc:Description>'
        '</cac:Response></cac:DocumentResponse>'
        '</ApplicationResponse>'
    )
    ruta = os.path.join(carpeta, f"R{RUC}-{tip}-{num}.zip")
    with zipfile.ZipFile(ruta, "w") as z:
        z.writestr(f"R{RUC}-{tip}-{num}.xml", xml)
    return ruta


RECHAZO = {"status": "RECHAZADO", "codigo": "2282",
           "descripcion": "Existe documento ya informado anteriormente", "lineas": []}
ACEPTADO = {"status": "ACEPTADO", "codigo": "0", "descripcion": None, "lineas": []}


# --- 1. el texto refleja lo que contesto SUNAT -------------------------------
print("\n[1] _veredicto_cdr dice la verdad")
texto = m._veredicto_cdr(RECHAZO)
check(texto.startswith("Rechazado"), f"un rechazo se lee como rechazo ({texto})")
check("2282" in texto, "y lleva el codigo de SUNAT")
check("Existe documento ya informado" in texto, "y el motivo")
check("Aceptado" not in texto, "sin decir 'Aceptado' en ningun lado")

texto = m._veredicto_cdr(ACEPTADO)
check(texto.startswith("Aceptado"), f"un aceptado sigue leyendose como aceptado ({texto})")

texto = m._veredicto_cdr({"status": "OBSERVADO", "codigo": "4095",
                          "descripcion": "El dato ingresado no cumple con el formato"})
check(texto.startswith("Observado"), f"y un observado como observado ({texto})")

largo = m._veredicto_cdr({"status": "RECHAZADO", "codigo": "2282", "descripcion": "x" * 900})
check(len(largo) <= m._MAX_DES_OBSE,
      f"se recorta a los {m._MAX_DES_OBSE} de DES_OBSE ({len(largo)})")


# --- 2. el camino del rechazo es el que rotulaba "Aceptado" ------------------
# _registrar_error_cdr() cierra el resumen igual --el ticket ya se consumio-- y era
# quien escribia el texto fijo. Es el caso exacto de los 39 del incidente.
print("\n[2] un resumen rechazado no se rotula 'Aceptado'")
ruta = bd_sfs(RC, "09")
m._registrar_resumen(RC, BOLETAS)
m._registrar_error_cdr(None, RC, RECHAZO)
situ, texto = obse(ruta, RC)
check(situ == "03", "sigue cerrandose en '03' (el ticket ya se consumio)")
check("Aceptado" not in (texto or ""), f"y NO dice 'Aceptado' ({texto})")
check("2282" in (texto or ""), "dice el codigo que devolvio SUNAT")
check("Rechazado" in (texto or ""), "y que fue un rechazo")


# --- 3. un resumen aceptado sigue igual (sin regresion) ---------------------
print("\n[3] un resumen aceptado sigue leyendose como aceptado")
ruta = bd_sfs(RC, "09")
m._registrar_resumen(RC, BOLETAS)
m._actualizar_sql_cdr(None, RC, ACEPTADO)
situ, texto = obse(ruta, RC)
check(situ == "03", "se cierra en '03'")
check("Aceptado" in (texto or ""), f"y dice Aceptado ({texto})")


# --- 4. cerrar sin un `parsed` a mano: se lee el CDR archivado --------------
# Es el rescate de recuperar_cdr_resumenes(): cierra porque el CDR existe, no porque
# haya visto que fue aceptado. Suponerlo es justamente lo que hacia mentir al campo.
print("\n[4] sin veredicto a mano, se lee del CDR archivado")
ruta = bd_sfs(RC, "05")
cdr_zip(m.DIR_PROCESADOS, m._TIPO_RC, RC, "2282", "Existe documento ya informado anteriormente")
m._cerrar_resumen_en_sfs(RUC, RC)
situ, texto = obse(ruta, RC)
check(situ == "03", "cierra igual")
check("2282" in (texto or ""), f"leyendo el veredicto real del ZIP ({texto})")
check("Aceptado" not in (texto or ""), "sin suponer que fue aceptado")

print("\n[5] sin CDR legible, no se inventa un veredicto")
os.remove(os.path.join(m.DIR_PROCESADOS, f"R{RUC}-{m._TIPO_RC}-{RC}.zip"))
ruta = bd_sfs(RC, "05")
m._cerrar_resumen_en_sfs(RUC, RC)
situ, texto = obse(ruta, RC)
check(situ == "03", "cierra igual: dejarlo abierto lo reconsultaria en vano")
check("Aceptado" not in (texto or ""), f"pero no afirma que fue aceptado ({texto})")


# --- 6. el mismo texto fijo estaba en _activar_pendientes_sfs_bd ------------
print("\n[6] activar_pendientes tampoco supone el veredicto")
ruta = bd_sfs("F003-000010", "01", tipo="01")
cdr_zip(m.SFS_RPTA_DIR, "01", "F003-000010", "2335", "El comprobante fue rechazado")
m._eliminar_data_files = lambda base: None
m.activar_procesamiento_sfs = lambda docs: None
m._activar_pendientes_sfs_bd(RUC, [])
situ, texto = obse(ruta, "F003-000010")
check(situ == "03", "cierra el documento que ya tiene CDR")
check("2335" in (texto or ""), f"con el codigo real ({texto})")
check("Aceptado" not in (texto or ""), "y no rotulado 'Aceptado'")


print()
if FALLAS:
    print(f"{FALLAS} FALLA(S)")
    sys.exit(1)
print("TODO OK")
