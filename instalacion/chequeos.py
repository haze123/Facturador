"""
Diagnostico de una instalacion: que pieza esta mal y por que.

Contesta la pregunta que se hace siempre cuando algo no anda ("por que no esta
emitiendo?") revisando cada parte por separado, en vez de dejar que haya que
deducirlo de los logs.

No modifica nada.
"""
import os
import sqlite3
import urllib.request
from datetime import datetime

# Estados del SFS que nadie resuelve solo: anulado, con errores y rechazado.
_BLOQUEADOS = ("05", "06", "10")
_NOMBRE_ESTADO = {
    "01": "por generar XML", "02": "XML generado", "03": "aceptado",
    "04": "aceptado c/obs", "05": "anulado", "06": "con errores",
    "07": "XML por validar", "08": "enviado, por procesar", "09": "procesando",
    "10": "rechazado", "11": "CDR descargado", "12": "CDR descargado c/obs",
}
_CAMPOS_SFS = (
    ("NUMRUC", "RUC"), ("RAZON", "razon social"), ("NOMCERT", "certificado"),
    ("UBIGEO", "ubigeo"), ("NOMCOM", "nombre comercial"), ("USUSOL", "usuario SOL"),
)


def datos_sfs(ruta_bd):
    """Configuracion del emisor guardada en el SFS. {} si no se puede leer."""
    if not os.path.exists(ruta_bd):
        return {}
    try:
        with sqlite3.connect(ruta_bd) as con:
            campos = tuple(c for c, _ in _CAMPOS_SFS)
            marcas = ",".join("?" * len(campos))
            cur = con.execute(
                f"SELECT COD_PARA, VAL_PARA FROM PARAMETRO WHERE COD_PARA IN ({marcas})",
                campos,
            )
            return {k: (v or "") for k, v in cur.fetchall()}
    except sqlite3.Error:
        return {}


def _leer_env(raiz):
    ruta = os.path.join(raiz, ".env")
    if not os.path.exists(ruta):
        return None
    conf = {}
    # utf-8-sig: tolera el BOM que deja el Bloc de notas.
    with open(ruta, encoding="utf-8-sig", errors="replace") as fh:
        for linea in fh:
            l = linea.strip()
            if not l or l.startswith("#") or "=" not in l:
                continue
            k, v = l.split("=", 1)
            conf[k.strip()] = v.strip()
    return conf


def verificar(raiz):
    """Imprime el diagnostico. Devuelve la cantidad de fallas."""
    from instalador import C, aviso, error, nota, ok, titulo

    fallas = avisos = 0

    def _falla(t):
        nonlocal fallas
        error(t); fallas += 1

    def _aviso(t):
        nonlocal avisos
        aviso(t); avisos += 1

    print(f"\n{C.NEGRITA}  VERIFICACION{C.FIN}")
    nota(f"  {datetime.now():%Y-%m-%d %H:%M:%S}")

    # --- 1. Prerrequisitos -------------------------------------------------
    import sistema
    titulo("1. Prerrequisitos")
    for nombre, presente, detalle, _ in sistema.revisar_prerrequisitos():
        (ok if presente else _falla)(detalle or nombre)
    if sistema.hay("pm2"):
        ok("PM2")
    else:
        _falla("PM2 no esta instalado")
    for paquete in ("psycopg2", "dotenv", "watchdog"):
        codigo, _ = sistema.correr(["python", "-c", f"import {paquete}"], timeout=120)
        (ok if codigo == 0 else _falla)(f"modulo de Python '{paquete}'")

    # --- 2. Configuracion del daemon ---------------------------------------
    titulo("2. Configuracion del daemon (.env)")
    conf = _leer_env(raiz)
    if conf is None:
        _falla("no existe el archivo .env")
        conf = {}
    else:
        for clave in ("DATABASE_URL", "EMISOR_RUC"):
            if conf.get(clave):
                # DATABASE_URL nunca se imprime: lleva la contrasena.
                ok(f"{clave} definida" if clave == "DATABASE_URL" else f"{clave} = {conf[clave]}")
            else:
                _falla(f"falta {clave} en el .env")
        # Sin credenciales SOL no se cierra ningun resumen diario: su CDR llega
        # detras de un ticket y no hay otra forma de traerlo.
        if conf.get("SOL_USUARIO") and conf.get("SOL_CLAVE"):
            ok(f"credenciales SOL configuradas (usuario {conf['SOL_USUARIO']})")
        else:
            _falla("faltan SOL_USUARIO / SOL_CLAVE: sin eso NO se cierra ningun resumen")

    ruta_bd_sfs = conf.get("SFS_BD_PATH") or r"C:\SFS_v-2.1\bd\BDFacturador.db"
    url_sfs = conf.get("SFS_BASE_URL") or "http://localhost:9000"
    dir_data = conf.get("SFS_DATA_DIR") or r"C:\SFS_v-2.1\sunat_archivos\sfs\DATA"

    # --- 3. Base de datos de la aplicacion ---------------------------------
    titulo("3. Base de datos de la aplicacion")
    if not conf.get("DATABASE_URL"):
        _aviso("sin DATABASE_URL no se puede probar la conexion")
    else:
        detalle = _revisar_bd(raiz)
        if detalle.get("error"):
            _falla(f"no se pudo conectar: {detalle['error']}")
        else:
            ok("conexion establecida")
            nota(f"         comprobantes sin enviar: {detalle['pendientes']}")
            desfase = detalle["desfase"]
            if desfase:
                ok(f"el reloj de la BD se corrige en {desfase} h para declarar a SUNAT")
            else:
                ok("la BD ya esta en hora local (sin correccion)")

    # --- 4. SFS -------------------------------------------------------------
    titulo("4. Facturador SUNAT (SFS)")
    try:
        urllib.request.urlopen(f"{url_sfs}/", timeout=8).close()
        ok(f"responde en {url_sfs}")
    except Exception:
        _falla(f"no responde en {url_sfs} - el daemon no puede entregarle nada")

    if not os.path.exists(ruta_bd_sfs):
        _falla(f"no se encontro la base del SFS en {ruta_bd_sfs}")
        sfs = {}
    else:
        ok("base del SFS encontrada")
        sfs = datos_sfs(ruta_bd_sfs)
        # Sin estos datos SUNAT observa el comprobante (p.ej. codigo 4092 por
        # nombre comercial vacio).
        for campo, etiqueta in _CAMPOS_SFS:
            if sfs.get(campo):
                ok(f"{etiqueta}: {sfs[campo]}")
            else:
                _falla(f"el SFS no tiene configurado: {etiqueta}")
        if conf.get("EMISOR_RUC") and sfs.get("NUMRUC") and conf["EMISOR_RUC"] != sfs["NUMRUC"]:
            _falla(f"el RUC del .env ({conf['EMISOR_RUC']}) NO coincide con el del SFS "
                   f"({sfs['NUMRUC']})")

    # --- 5. Ambiente --------------------------------------------------------
    titulo("5. Ambiente de SUNAT")
    activa = _ambiente(dir_data)
    if activa is None:
        _falla("no se encontro constantes.properties")
    elif "e-beta" in activa:
        _aviso("BETA - los comprobantes NO tienen validez fiscal")
        nota("         Ningun comprobante real debe emitirse en este estado.")
        nota(f"         {activa}")
    elif "e-factura" in activa:
        ok("PRODUCCION - los comprobantes tienen validez fiscal")
        nota(f"         {activa}")
    else:
        _falla(f"no se pudo determinar el ambiente: {activa}")

    # --- 6. Certificado -----------------------------------------------------
    titulo("6. Certificado digital")
    dir_cert = os.path.join(os.path.dirname(dir_data), "CERT")
    certs = []
    if os.path.isdir(dir_cert):
        certs = [f for f in os.listdir(dir_cert) if f.lower().endswith((".p12", ".pfx"))]
    if not certs:
        _falla(f"no hay ningun certificado en {dir_cert}")
    else:
        for c in certs:
            tam = os.path.getsize(os.path.join(dir_cert, c))
            ok(f"{c} ({tam / 1024:.1f} KB)")
        nota("         Su vencimiento se comprueba al configurar el cliente.")

    # --- 7. Procesos --------------------------------------------------------
    titulo("7. Procesos (PM2)")
    procesos = _procesos_pm2()
    if procesos is None:
        _falla("no se pudo leer la lista de procesos de PM2")
    else:
        for nombre in ("sfs", "facturador"):
            p = procesos.get(nombre)
            if p is None:
                _falla(f"'{nombre}' no esta en PM2 (pm2 start sfs.config.js)")
            elif p["estado"] != "online":
                _falla(f"'{nombre}' esta en estado '{p['estado']}'")
            else:
                ok(f"'{nombre}' online ({p['horas']:.1f}h, {p['reinicios']} reinicios)")
                # Reinicios repetidos suelen ser un crash en bucle.
                if p["reinicios"] > 20:
                    _aviso(f"'{nombre}' se reinicio {p['reinicios']} veces: revisar sus logs")

    # --- 8. Trabajo pendiente ----------------------------------------------
    titulo("8. Estado del trabajo")
    estados = _estados_sfs(ruta_bd_sfs)
    if estados is None:
        _aviso("no se pudo leer la bandeja del SFS")
    elif not estados:
        ok("la bandeja del SFS esta vacia (aun no se emitio ningun comprobante)")
    else:
        bloqueados = 0
        for situ, cantidad in sorted(estados.items()):
            etiqueta = _NOMBRE_ESTADO.get(situ, situ)
            if situ in _BLOQUEADOS:
                _aviso(f"{cantidad} documento(s) BLOQUEADOS: {etiqueta}")
                bloqueados += cantidad
            else:
                nota(f"         {cantidad} documento(s): {etiqueta}")
        if not bloqueados:
            ok("ningun documento bloqueado")

    if os.path.isdir(dir_data):
        viejos = _archivos_viejos(dir_data)
        if viejos:
            _aviso(f"{viejos} archivo(s) en DATA de hace mas de 2 dias: quedaron sin cerrar")
        else:
            ok("DATA sin archivos atascados")

    # --- Resumen ------------------------------------------------------------
    print(f"\n{C.GRIS}{'=' * 58}{C.FIN}")
    if not fallas and not avisos:
        print(f"  {C.VERDE}TODO CORRECTO{C.FIN}")
    elif not fallas:
        print(f"  {C.AMAR}FUNCIONA, con {avisos} aviso(s) para revisar{C.FIN}")
    else:
        print(f"  {C.ROJO}{fallas} FALLA(S) y {avisos} aviso(s){C.FIN}")
        print(f"  {C.ROJO}El facturador NO va a emitir correctamente hasta corregirlas.{C.FIN}")
    print(f"{C.GRIS}{'=' * 58}{C.FIN}")
    return fallas


# --- consultas ---------------------------------------------------------------

# Se corre con el Python INSTALADO en la PC, no dentro de este programa. Dos
# razones: compilado a .exe no estan las librerias del daemon (psycopg2, xml), y
# sobre todo, lo que hay que verificar es que el daemon pueda conectarse — y el
# daemon corre con ese Python, no con este.
_GUION_BD = """
import json, logging, sys
sys.path.insert(0, sys.argv[1])
try:
    import main as m
except Exception as e:
    print(json.dumps({"error": "no se pudo cargar main.py: %s" % e})); raise SystemExit
# main.py escribe en facturador.log al importarse: sin silenciarlo, cada
# verificacion dejaria rastro en el registro de emision real.
r = logging.getLogger()
for h in list(r.handlers):
    if isinstance(h, logging.FileHandler): r.removeHandler(h)
r.addHandler(logging.NullHandler()); r.setLevel(logging.CRITICAL)
con = None
try:
    con = m.conectar_bd()
    c = con.cursor()
    c.execute('SELECT count(*) FROM public."Factura" WHERE enviado IS NOT TRUE')
    print(json.dumps({"pendientes": c.fetchone()[0], "desfase": m.detectar_desfase_bd(con)}))
except Exception as e:
    print(json.dumps({"error": "%s: %s" % (type(e).__name__, str(e).strip().splitlines()[0])}))
finally:
    if con: con.close()
"""


def _revisar_bd(raiz):
    """Conexion, pendientes y desfase horario, vistos por el daemon."""
    import json
    import sistema
    codigo, salida = sistema.correr(["python", "-c", _GUION_BD, raiz], timeout=120)
    for linea in reversed(salida.strip().splitlines()):
        linea = linea.strip()
        if linea.startswith("{"):
            try:
                return json.loads(linea)
            except ValueError:
                pass
    return {"error": (salida.strip().splitlines() or ["sin respuesta"])[-1]}


def _ambiente(dir_data):
    archivo = os.path.join(os.path.dirname(dir_data), "VALI", "constantes.properties")
    if not os.path.exists(archivo):
        return None
    with open(archivo, encoding="utf-8-sig", errors="replace") as fh:
        for linea in fh:
            l = linea.strip()
            if l.startswith("RUTA_SERV_CDP="):
                return l.split("=", 1)[1].strip()
    return ""


def _estados_sfs(ruta_bd):
    if not os.path.exists(ruta_bd):
        return None
    try:
        with sqlite3.connect(ruta_bd) as con:
            cur = con.execute("SELECT IND_SITU, COUNT(*) FROM DOCUMENTO GROUP BY IND_SITU")
            return {(s or "").strip(): n for s, n in cur.fetchall()}
    except sqlite3.Error:
        return None


def _procesos_pm2():
    """
    {nombre: {estado, reinicios, horas}}. Se parsea con json de Python porque el
    ConvertFrom-Json de PowerShell no puede con la salida de 'pm2 jlist': incluye
    las variables de entorno de Windows, con 'username' y 'USERNAME' duplicadas.
    """
    import json
    import sistema
    codigo, salida = sistema.correr("pm2 jlist", timeout=90)
    inicio = salida.find("[")
    if inicio < 0:
        return None
    try:
        procesos = json.loads(salida[inicio:])
    except ValueError:
        return None
    ahora = datetime.now().timestamp() * 1000
    resultado = {}
    for p in procesos:
        entorno = p.get("pm2_env", {})
        arranque = entorno.get("pm_uptime", 0)
        resultado[p.get("name", "?")] = {
            "estado": entorno.get("status", "?"),
            "reinicios": entorno.get("restart_time", 0),
            "horas": max(0, (ahora - arranque) / 3600000),
        }
    return resultado


def _archivos_viejos(dir_data, dias=2):
    limite = datetime.now().timestamp() - dias * 86400
    try:
        return sum(
            1 for f in os.listdir(dir_data)
            if os.path.isfile(os.path.join(dir_data, f))
            and os.path.getmtime(os.path.join(dir_data, f)) < limite
        )
    except OSError:
        return 0
