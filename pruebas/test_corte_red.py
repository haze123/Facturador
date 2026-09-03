import os, sys, sqlite3, tempfile, time, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["SFS_DATA_DIR"] = tempfile.mkdtemp()
import main, logging
logging.disable(logging.CRITICAL)

TMP = tempfile.mkdtemp()
main._REINTENTOS_PATH = os.path.join(TMP, "reintentos.json")
main.SFS_RPTA_DIR = os.path.join(TMP, "RPTA")
RUC = "20612016527"
RED  = "Hubo un problema al invocar servicio SUNAT: Could not send Message."
DATO = "0111 - No tiene el perfil para enviar comprobantes electronicos"

MARCADOS, CDRS = [], []
class FakeBD:
    @staticmethod
    def marcar_enviado(conn, num, enviado=True, limpiar_error=True):
        MARCADOS.append(num); return 1
main._bd = lambda: FakeBD()
main._guardar_cdr = lambda ruc, tip, num, cdr, msg: CDRS.append(num)

ESTADO = {"valor": ("no_registrado", None, "no lo tiene")}
main.estado_en_sunat = lambda ruc, tip, num: ESTADO["valor"]

def bd(filas):
    ruta = os.path.join(TMP, f"s{time.time_ns()}.db")
    c = sqlite3.connect(ruta)
    c.execute("CREATE TABLE DOCUMENTO (NUM_RUC TEXT, TIP_DOCU TEXT, NUM_DOCU TEXT, IND_SITU TEXT, DES_OBSE TEXT)")
    c.executemany("INSERT INTO DOCUMENTO VALUES (?,?,?,?,?)", filas)
    c.commit(); c.close()
    main.SFS_BD_PATH = ruta
    return ruta

def quedan(ruta):
    c = sqlite3.connect(ruta); r = dict(c.execute("SELECT NUM_DOCU, IND_SITU FROM DOCUMENTO")); c.close(); return r

def reset():
    MARCADOS.clear(); CDRS.clear()
    if os.path.exists(main._REINTENTOS_PATH): os.remove(main._REINTENTOS_PATH)

def sin_espera(num):
    with open(main._REINTENTOS_PATH) as f: d = json.load(f)
    d[num]["esperar_hasta"] = 0
    with open(main._REINTENTOS_PATH, "w") as f: json.dump(d, f)

FALLAS = 0
def check(c, m):
    global FALLAS
    if not c: FALLAS += 1
    print(("  OK    " if c else "  FALLA "), m)

# 1) un '06' de red reintenta MAS ALLA de 3 y no queda bloqueado
reset()
for vuelta in range(6):
    bd([(RUC, "01", "F003-1", "06", RED)])
    main.resetear_rechazados(None, RUC)
    if os.path.exists(main._REINTENTOS_PATH): sin_espera("F003-1")
check(MARCADOS.count("F003-1") == 6, f"reintento 6 veces, mas que el tope de 3 ({MARCADOS.count('F003-1')})")
reg = json.load(open(main._REINTENTOS_PATH))["F003-1"]
check(reg.get("intentos") is None, "no consumio presupuesto de reintentos ('intentos' ausente)")
check(reg["cortes"] == 6, f"lleva la cuenta aparte en 'cortes' ({reg['cortes']})")

# 2) un '06' de dato agota los 3 y queda bloqueado (sin regresion)
reset()
for vuelta in range(5):
    ruta = bd([(RUC, "01", "F003-2", "06", DATO)])
    main.resetear_rechazados(None, RUC)
check(MARCADOS.count("F003-2") == 3, f"solo 3 reenvios ({MARCADOS.count('F003-2')})")
check(quedan(ruta).get("F003-2") == "06", "queda en DOCUMENTO para el reporte de bloqueados")

# 3) el '10' se comporta igual que hoy
reset()
for vuelta in range(5):
    ruta = bd([(RUC, "01", "F003-3", "10", "rechazado por SUNAT")])
    main.resetear_rechazados(None, RUC)
check(MARCADOS.count("F003-3") == 3, f"el '10' sigue con tope de 3 ({MARCADOS.count('F003-3')})")

# 4) el '05' (anulado) no se toca
reset()
ruta = bd([(RUC, "03", "B001-4", "05", "anulado")])
main.resetear_rechazados(None, RUC)
check(MARCADOS == [], "el anulado no se reenvia")
check(quedan(ruta).get("B001-4") == "05", "y sigue en DOCUMENTO")

# 5) el backoff persiste a un reinicio del proceso
reset()
bd([(RUC, "01", "F003-5", "06", RED)])
main.resetear_rechazados(None, RUC)
guardado = json.load(open(main._REINTENTOS_PATH))["F003-5"]
check(guardado["esperar_hasta"] > time.time(), "la espera quedo guardada en disco")
MARCADOS.clear()
bd([(RUC, "01", "F003-5", "06", RED)])   # simula el ciclo tras un reinicio de PM2
main.resetear_rechazados(None, RUC)
check(MARCADOS == [], "tras 'reiniciar', respeta la espera y no reenvia")

# 6) si SUNAT SI lo tiene registrado, no se reenvia: se recupera su CDR
reset()
ESTADO["valor"] = ("registrado", b"zip", "lo tiene")
ruta = bd([(RUC, "01", "F003-6", "06", RED)])
main.resetear_rechazados(None, RUC)
check(MARCADOS == [], "un comprobante que SUNAT ya tiene NO se reenvia")
check(CDRS == ["F003-6"], "se recupera su CDR")
check(quedan(ruta).get("F003-6") == "06", "y no se borra de la bandeja")

# 6b) 'desconocido' tampoco habilita el reenvio
reset()
ESTADO["valor"] = ("desconocido", None, "sin respuesta")
main.resetear_rechazados(None, RUC)
check(MARCADOS == [], "un 'desconocido' NO habilita el reenvio")
ESTADO["valor"] = ("no_registrado", None, "no lo tiene")

# 7) al aceptarse, se limpia el contador y la espera
reset()
bd([(RUC, "01", "F003-7", "06", RED)])
main.resetear_rechazados(None, RUC)
check("F003-7" in json.load(open(main._REINTENTOS_PATH)), "quedo registrado")
main._limpiar_reintento("F003-7")
check("F003-7" not in json.load(open(main._REINTENTOS_PATH)), "al aceptarse se limpia todo (contador y espera)")

if FALLAS:
    print(str(FALLAS) + ' FALLA(S)')
    sys.exit(1)
print('TODO OK')
