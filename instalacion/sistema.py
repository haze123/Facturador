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
        # No alcanza con que haya Java: las versiones anteriores a la 8u242
        # rechazan certificados PKCS#12 con codificaciones que las nuevas aceptan
        # sin problema. Verificado en una instalacion real: con 1.8.0_202 el SFS
        # respondia "el certificado no fue creado" con un .p12 perfectamente
        # valido, byte a byte identico a uno que funciona con 1.8.0_492.
        "comando": "java", "nombre": "Java 8 (Temurin)",
        "winget": "EclipseAdoptium.Temurin.8.JRE", "manual": "https://adoptium.net/",
        # 1.8.0_242: en Java el numero de actualizacion es el CUARTO componente.
        "version": ("-version", (1, 8, 0, 242)),
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

    OJO con el primer elemento: en Windows, pm2, npm, gh y winget no son .exe sino
    archivos .CMD, y Windows no sabe ejecutarlos sin pasar por el shell. Invocarlos
    con una lista (que Python corre sin shell) falla con "no se puede encontrar el
    archivo" aunque esten instalados y en el PATH. Por eso se resuelve la ruta real
    con which() y, si termina en .cmd o .bat, se ejecuta a traves de cmd.exe.
    """
    if isinstance(comando, (list, tuple)):
        comando = list(comando)
        ruta = shutil.which(comando[0])
        if ruta:
            comando[0] = ruta
            if ruta.lower().endswith((".cmd", ".bat")):
                comando = ["cmd", "/c"] + comando
    try:
        r = subprocess.run(
            comando, shell=isinstance(comando, str), capture_output=True,
            text=True, errors="replace", timeout=timeout,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"el comando tardo mas de {timeout} segundos"
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def hay(comando):
    return shutil.which(comando) is not None


def _version_de(comando, argumento):
    codigo, salida = correr([comando, argumento], timeout=60)
    return salida.strip().splitlines()[0] if salida.strip() else ""


def _numeros_de_version(texto):
    """
    Los numeros de una version, como tupla comparable.

    Contempla el formato de Java, donde la actualizacion va despues de un guion
    bajo ("1.8.0_202" -> (1,8,0,202)) y no de un punto como en todo lo demas.
    """
    import re
    m = re.search(r"(\d+(?:\.\d+)*(?:_\d+)?)", texto)
    if not m:
        return ()
    return tuple(int(p) for p in m.group(1).replace("_", ".").split("."))


def _cumple_version(comando, argumento, minima):
    numeros = _numeros_de_version(_version_de(comando, argumento))
    if not numeros:
        return False
    # Se comparan solo tantas partes como pide el minimo: "3.14" cumple (3,10).
    return numeros[:len(minima)] >= tuple(minima)


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
        # El ZIP de SUNAT no trae carpeta contenedora: en su raiz estan directamente
        # facturadorApp-x.y.jar, prod.yaml, bd/ y sunat_archivos/. Descomprimir en el
        # padre --esperando que el ZIP creara esa carpeta-- volcaba todo eso suelto en
        # la raiz del disco, y el jar nunca aparecia donde se lo buscaba despues.
        os.makedirs(ruta_destino, exist_ok=True)
        with zipfile.ZipFile(zip_tmp) as z:
            z.extractall(ruta_destino)
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

    sobrantes = _limpiar_data_de_ejemplo(ruta_destino)
    if sobrantes:
        avisar(f"    se quitaron {sobrantes} archivo(s) de ejemplo que traia el ZIP")
    return True, f"instalado en {ruta_destino}"


def _limpiar_data_de_ejemplo(ruta_sfs):
    """
    Vacia DATA de los comprobantes que SUNAT deja en su ZIP.

    La distribucion viene con archivos de prueba de OTRO contribuyente (se vieron
    con RUC 20480072872). El SFS los levanta como documentos propios e intenta
    emitirlos, asi que no pueden quedar en una instalacion nueva.
    """
    data = os.path.join(ruta_sfs, "sunat_archivos", "sfs", "DATA")
    if not os.path.isdir(data):
        return 0
    borrados = 0
    for nombre in os.listdir(data):
        ruta = os.path.join(data, nombre)
        # .gitkeep y cualquier subcarpeta se conservan: solo se van los comprobantes.
        if os.path.isfile(ruta) and not nombre.startswith("."):
            try:
                os.remove(ruta)
                borrados += 1
            except OSError:
                pass
    return borrados


def instancias_sql():
    """
    Nombres de las instancias de SQL Server instaladas en esta PC.

    Sirve para no tener que adivinarlas: SQL Server Express se instala como
    'SQLEXPRESS' y quien configura el daemon rara vez sabe eso de memoria. Lista
    vacia si no hay ninguna o no se puede leer el registro.
    """
    try:
        import winreg
    except ImportError:
        return []
    try:
        clave = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Microsoft SQL Server\Instance Names\SQL")
    except OSError:
        return []
    nombres, i = [], 0
    while True:
        try:
            nombre, _, _ = winreg.EnumValue(clave, i)
        except OSError:
            break
        nombres.append(nombre)
        i += 1
    return nombres
