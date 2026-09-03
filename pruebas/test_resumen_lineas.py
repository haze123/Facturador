import os, sys, tempfile, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SFS_DATA_DIR"] = tempfile.mkdtemp()
import main, logging
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp()
main._RESUMENES_PATH = os.path.join(TMP, "resumenes.json")
main._REINTENTOS_PATH = os.path.join(TMP, "reintentos.json")
main.MAX_REINTENTOS_RECHAZO = 3

CAP = {"guardados": [], "marcados": []}
def fake_guardar_error(conn, num, detalle):
    CAP["guardados"].append((num, detalle))
    return 1
def fake_marcar_enviados(conn, nums):
    CAP["marcados"].extend(nums)
    return len(nums)

class FakeBD:
    guardar_error = staticmethod(fake_guardar_error)
    marcar_enviados = staticmethod(fake_marcar_enviados)
main._bd = lambda: FakeBD()
main._escribir_bd = lambda fn, conn, *a: fn(conn, *a)
main._cerrar_resumen_en_sfs = lambda ruc, num: None
main.EMISOR_RUC_OVERRIDE = "20609785269"

def reset():
    CAP["guardados"].clear(); CAP["marcados"].clear()
    for p in (main._RESUMENES_PATH, main._REINTENTOS_PATH):
        if os.path.exists(p): os.remove(p)

FALLAS = 0
def check(cond, msg):
    global FALLAS
    if not cond: FALLAS += 1
    print(("  OK    " if cond else "  FALLA "), msg)
    return cond

# --- caso 1: resumen limpio, sin ningun codigo de linea ---
reset()
boletas = [f"B003-{i:06d}" for i in range(1, 201)]
main._registrar_resumen("RC-20260826-001", boletas)
parsed = {"status": "ACEPTADO", "lineas": []}
ok = main._actualizar_sql_cdr(None, "RC-20260826-001", parsed)
check(ok is True, "devuelve True")
check(set(CAP["marcados"]) == set(boletas), "las 200 se marcan enviado=1")
check(not CAP["guardados"], "no se guarda ningun motivo")

# --- caso 2: 199 limpias, 1 con codigo de linea (el escenario del usuario) ---
reset()
boletas = [f"B003-{i:06d}" for i in range(1, 201)]
main._registrar_resumen("RC-20260826-002", boletas)
parsed = {"status": "ACEPTADO", "lineas": [("B003-000150", "3103", "Boleta ya declarada")]}
ok = main._actualizar_sql_cdr(None, "RC-20260826-002", parsed)
check(ok is True, "el resumen igual se cierra (devuelve True)")
check("B003-000150" not in CAP["marcados"], "la boleta con codigo NO se marca enviado=1")
check(len(CAP["marcados"]) == 199, f"las otras 199 si se marcan ({len(CAP['marcados'])})")
check(any("B003-000150" in n for n, _ in CAP["guardados"]), "se guarda el motivo de la excluida")
check(main._reintentos_de("B003-000150") == 1, "queda contado como 1er intento")

# --- caso 3: la misma boleta vuelve a fallar 2 veces mas -> agota reintentos ---
reset()
boletas = ["B003-000150"] + [f"B003-{i:06d}" for i in range(1, 200)]
for intento in range(1, 4):
    main._registrar_resumen(f"RC-20260826-00{intento+2}", boletas)
    parsed = {"status": "ACEPTADO", "lineas": [("B003-000150", "3103", "Boleta ya declarada")]}
    main._actualizar_sql_cdr(None, f"RC-20260826-00{intento+2}", parsed)
print(f"  -> tras 3 intentos, _reintentos_de = {main._reintentos_de('B003-000150')}")
check(main._reintentos_de("B003-000150") >= main.MAX_REINTENTOS_RECHAZO, "agoto los 3 reintentos")

# --- caso 4: una boleta agotada no vuelve a proponerse en un resumen nuevo ---
class FakeBDPendientes:
    @staticmethod
    def pendientes(conn):
        return [{
            "id": 1, "tipo_comprobante": "03", "tipo_enum": None,
            "numeracion_comprobante": "B003-000150",
            "fecha_emision": __import__("datetime").datetime(2026, 8, 25, 10, 0),
            "total": 30.0, "gravadas": 25.42, "igv": 4.58,
            "monto_letras": None,
        }]
main._bd = lambda: FakeBDPendientes()
candidatas = main.obtener_boletas_para_resumen(None)
check(candidatas == [], f"la boleta agotada no entra al pool de un resumen nuevo ({candidatas})")

# --- caso 5: TODAS las boletas del resumen vienen con codigo (caso extremo) ---
main._bd = lambda: FakeBD()
reset()
boletas = [f"B003-{i:06d}" for i in range(1, 6)]
main._registrar_resumen("RC-20260826-010", boletas)
parsed = {"status": "ACEPTADO", "lineas": [(b, "3103", "obs") for b in boletas]}
ok = main._actualizar_sql_cdr(None, "RC-20260826-010", parsed)
check(ok is True, "el resumen se cierra igual (no es un error de BD)")
check(CAP["marcados"] == [], "ninguna se marca enviado=1")
check(len(CAP["guardados"]) == 5, "las 5 quedan con motivo guardado")

if FALLAS:
    print(str(FALLAS) + ' FALLA(S)')
    sys.exit(1)
print('TODO OK')
