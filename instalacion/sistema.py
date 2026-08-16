"""
Operaciones sobre el entorno: prerrequisitos, descarga del SFS, PM2.

Separado de la interfaz para poder probarlo sin abrir el menu.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

# La 2.1 es la unica version contra la que se verifico el formato de los archivos
# planos que genera el daemon, incluido el del resumen diario de boletas.
# Cambiarla exige volver a probar la emision de cada tipo de comprobante.
VERSION_SFS = "2.1"
RUTA_SFS_POR_DEFECTO = r"C:\SFS_v-2.1"

# Descarga publica y directa de SUNAT, sin clave SOL. El nombre lleva un guion
# despues de la 'v' — no es un error de tipeo.
URL_SFS = "http://www2.sunat.gob.pe/facturador/SFS_v-{version}.zip"

# Que hace falta, y con que paquete de winget se resuelve cada uno.
PRERREQUISITOS = (
    {
        "comando": "python", "nombre": "Python 3.12",
        "winget": "Python.Python.3.12", "manual": "https://www.python.org/downloads/",
        "version": ("--version", (3, 10)),
    },
    {
        "comando": "java", "nombre": "Java 8 (Temurin)",
        "winget": "EclipseAdoptium.Temurin.8.JRE", "manual": "https://adoptium.net/",
    },
    {
        "comando": "node", "nombre": "Node.js LTS",
        "winget": "OpenJS.NodeJS.LTS", "manual": "https://nodejs.org/",
    },
)


def correr(comando, timeout=900):
    """
    Ejecuta un programa y devuelve (codigo, salida). Junta stdout y stderr porque
    varios (java, npm, pip) escriben informacion util en el segundo.
    """
    try:
        r = subprocess.run(
            comando, shell=isinstance(comando, str), capture_output=True,
            text=True, errors="replace", timeout=timeout,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"el comando tardo mas de {timeout} segundos"
    except Exception as e:
        return -1, str(e)


def hay(comando):
    return shutil.which(comando) is not None


def _version_de(comando, argumento):
    codigo, salida = correr([comando, argumento], timeout=60)
    return salida.strip().splitlines()[0] if salida.strip() else ""


def _cumple_version(comando, argumento, minima):
    import re
    texto = _version_de(comando, argumento)
    m = re.search(r"(\d+)\.(\d+)", texto)
    return bool(m) and tuple(int(x) for x in m.groups()) >= minima


def refrescar_path():
    """
    Vuelve a leer el PATH del registro. Un instalador lo modifica para los
    procesos NUEVOS, pero este ya arranco con el suyo: sin esto habria que cerrar
    y reabrir el programa despues de cada instalacion.
    """
    try:
        import winreg
        partes = []
        for raiz, clave in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        ):
            try:
                with winreg.OpenKey(raiz, clave) as k:
                    partes.append(winreg.QueryValueEx(k, "Path")[0])
            except OSError:
                pass
        if partes:
            os.environ["PATH"] = os.path.expandvars(";".join(partes))
    except Exception:
        pass   # si falla, solo hace falta reabrir el programa


def revisar_prerrequisitos():
    """[(nombre, instalado, detalle, requisito)] para cada prerrequisito."""
    estado = []
    for req in PRERREQUISITOS:
        presente = hay(req["comando"])
        detalle = ""
        if presente:
            arg, minima = req.get("version", ("--version", None))
            detalle = _version_de(req["comando"], "-version" if req["comando"] == "java" else arg)
            # Estar en el PATH no alcanza si la version es muy vieja: main.py usa
            # sintaxis de Python 3.10 en adelante.
            if minima and not _cumple_version(req["comando"], arg, minima):
                presente = False
                detalle += "  (version anterior a la minima)"
        estado.append((req["nombre"], presente, detalle, req))
    return estado


def instalar_con_winget(req, avisar=print):
    if not hay("winget"):
        return False, "winget no esta disponible en esta version de Windows"
    avisar(f"    instalando {req['nombre']}...")
    codigo, salida = correr([
        "winget", "install", "--id", req["winget"], "--exact", "--silent",
        "--disable-interactivity", "--accept-package-agreements", "--accept-source-agreements",
    ])
    refrescar_path()
    if hay(req["comando"]):
        return True, ""
    # Algunos instaladores dejan el PATH listo recien para el proximo proceso.
    return False, "se instalo pero aun no aparece; hay que cerrar y volver a abrir este programa"


def instalar_dependencias_python(requirements):
    codigo, salida = correr([sys.executable if not getattr(sys, "frozen", False) else "python",
                             "-m", "pip", "install", "-r", requirements,
                             "--disable-pip-version-check"])
    faltan = []
    for paquete in ("psycopg2", "dotenv", "watchdog"):
        c, _ = correr(["python", "-c", f"import {paquete}"], timeout=120)
        if c != 0:
            faltan.append(paquete)
    return (not faltan), (salida if faltan else ""), faltan


def instalar_pm2():
    """PM2 y el paquete que lo hace arrancar con Windows."""
    pasos = []
    if not hay("pm2"):
        correr(["npm", "install", "-g", "pm2"])
        refrescar_path()
    pasos.append(("PM2", hay("pm2")))

    # Windows no tiene init system, asi que 'pm2 startup' no alcanza.
    if not hay("pm2-startup"):
        correr(["npm", "install", "-g", "pm2-windows-startup"])
        refrescar_path()
    pasos.append(("arranque con Windows", hay("pm2-startup")))
    return pasos


def descargar_sfs(ruta_destino, version=VERSION_SFS, avisar=print):
    """Descarga el SFS de SUNAT y lo descomprime. Devuelve (ok, mensaje)."""
    jar = os.path.join(ruta_destino, f"facturadorApp-{version}.jar")
    if os.path.exists(jar):
        return True, f"ya instalado en {ruta_destino}"

    url = URL_SFS.format(version=version)
    zip_tmp = os.path.join(tempfile.gettempdir(), f"SFS_v-{version}.zip")
    avisar(f"    descargando de SUNAT (~90 MB)...")
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(zip_tmp, "wb") as fh:
            total = int(r.headers.get("Content-Length", 0))
            leido = 0
            while True:
                trozo = r.read(1024 * 256)
                if not trozo:
                    break
                fh.write(trozo)
                leido += len(trozo)
                if total:
                    avisar(f"\r    {leido * 100 // total}%  ({leido // 1048576} MB)", fin="")
        avisar("")
    except Exception as e:
        return False, f"no se pudo descargar: {e}"

    # Que sea realmente un ZIP: SUNAT devuelve una pagina de error con codigo 200
    # si la version no existe.
    with open(zip_tmp, "rb") as fh:
        if fh.read(2) != b"PK":
            os.remove(zip_tmp)
            return False, f"lo descargado no es un ZIP; puede que la version {version} ya no exista"

    avisar("    descomprimiendo...")
    try:
        padre = os.path.dirname(ruta_destino.rstrip("\\/")) or "C:\\"
        os.makedirs(padre, exist_ok=True)
        with zipfile.ZipFile(zip_tmp) as z:
            z.extractall(padre)
    except Exception as e:
        return False, f"no se pudo descomprimir: {e}"
    finally:
        if os.path.exists(zip_tmp):
            os.remove(zip_tmp)

    if not os.path.exists(jar):
        return False, f"el ZIP no dejo {jar}"

    # Las carpetas de trabajo tienen que existir antes de emitir.
    for carpeta in ("DATA", "RPTA", "CERT"):
        os.makedirs(os.path.join(ruta_destino, "sunat_archivos", "sfs", carpeta), exist_ok=True)
    return True, f"instalado en {ruta_destino}"
