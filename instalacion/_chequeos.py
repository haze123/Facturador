"""
Chequeos del lado Python para verificar.ps1.

Vive aparte y no dentro del script de PowerShell a proposito: incrustar Python
multilinea en un here-string de PowerShell hace que las comillas y los '$' se
pisen entre si, y los errores aparecen como sintaxis rota en tiempo de ejecucion.
Ademas asi se puede correr y depurar solo.

Imprime una linea 'clave=valor' por dato. La clave 'error' indica que ese bloque
no se pudo evaluar; el resto de los chequeos sigue igual.
"""
import os
import sqlite3
import sys

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Estados del SFS que nadie resuelve solo: anulado, con errores y rechazado.
_BLOQUEADOS = ("05", "06", "10")


def _emitir(clave, valor):
    # Los saltos de linea romperian el formato de una linea por dato.
    print(f"{clave}={str(valor).replace(chr(10), ' ').strip()}")


def revisar_bd_aplicacion():
    """Conexion, pendientes y el desfase horario con el que se declara a SUNAT."""
    sys.path.insert(0, _BASE)
    try:
        import main as m
    except Exception as e:
        _emitir("bd_error", f"no se pudo cargar main.py: {e}")
        return

    # main.py configura el log hacia facturador.log al importarse: sin esto, cada
    # verificacion dejaria rastro en el registro de emision real, que es donde se
    # investiga que paso con un comprobante.
    import logging
    raiz = logging.getLogger()
    for h in list(raiz.handlers):
        if isinstance(h, logging.FileHandler):
            raiz.removeHandler(h)
    raiz.addHandler(logging.NullHandler())
    raiz.setLevel(logging.CRITICAL)

    conn = None
    try:
        conn = m.conectar_bd()
        cur = conn.cursor()
        cur.execute('SELECT count(*) FROM public."Factura" WHERE enviado IS NOT TRUE')
        _emitir("bd_pendientes", cur.fetchone()[0])
        _emitir("bd_desfase", m.detectar_desfase_bd(conn))
        _emitir("bd_ok", "1")
    except Exception as e:
        _emitir("bd_error", f"{type(e).__name__}: {e}")
    finally:
        if conn:
            conn.close()


def revisar_sfs(ruta_bd):
    """Datos del emisor cargados en el SFS y estado de su bandeja."""
    if not os.path.exists(ruta_bd):
        _emitir("sfs_error", f"no existe {ruta_bd}")
        return
    try:
        with sqlite3.connect(ruta_bd) as con:
            cur = con.cursor()
            campos = ("NUMRUC", "RAZON", "NOMCERT", "UBIGEO", "NOMCOM", "USUSOL")
            marcas = ",".join("?" * len(campos))
            cur.execute(
                f"SELECT COD_PARA, VAL_PARA FROM PARAMETRO WHERE COD_PARA IN ({marcas})",
                campos,
            )
            valores = dict(cur.fetchall())
            for campo in campos:
                _emitir(f"sfs_{campo}", valores.get(campo, ""))

            cur.execute("SELECT IND_SITU, COUNT(*) FROM DOCUMENTO GROUP BY IND_SITU")
            filas = cur.fetchall()
            _emitir("sfs_estados", ";".join(f"{s}:{n}" for s, n in filas))
            _emitir(
                "sfs_bloqueados",
                sum(n for s, n in filas if (s or "").strip() in _BLOQUEADOS),
            )
    except Exception as e:
        _emitir("sfs_error", f"{type(e).__name__}: {e}")


def revisar_pm2():
    """
    Estado de los procesos gestionados por PM2.

    Se parsea acá y no en PowerShell porque su ConvertFrom-Json falla con la
    salida de 'pm2 jlist': incluye las variables de entorno de Windows, que traen
    'username' y 'USERNAME', y para PowerShell son la misma clave duplicada.
    """
    import json
    import subprocess

    try:
        # shell=True porque en Windows pm2 es un .cmd, no un ejecutable.
        salida = subprocess.run(
            "pm2 jlist", shell=True, capture_output=True, text=True, timeout=60
        ).stdout
        inicio = salida.find("[")
        if inicio < 0:
            _emitir("pm2_error", "pm2 no devolvio una lista de procesos")
            return
        procesos = json.loads(salida[inicio:])
    except Exception as e:
        _emitir("pm2_error", f"{type(e).__name__}: {e}")
        return

    for p in procesos:
        entorno = p.get("pm2_env", {})
        nombre = p.get("name", "?")
        _emitir(
            f"pm2_{nombre}",
            "{}|{}|{}".format(
                entorno.get("status", "?"),
                entorno.get("restart_time", 0),
                entorno.get("pm_uptime", 0),
            ),
        )
    _emitir("pm2_ok", "1")


if __name__ == "__main__":
    revisar_bd_aplicacion()
    revisar_sfs(sys.argv[1] if len(sys.argv) > 1 else r"C:\SFS_v-2.1\bd\BDFacturador.db")
    revisar_pm2()
