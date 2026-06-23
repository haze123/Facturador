"""
Instalador del Robot SUNAT.
Ejecutar UNA SOLA VEZ en cada PC donde se instale el programa.
"""
import os
import sys
import subprocess
import shutil

DIRECTORIO = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Utilidades de consola
# ---------------------------------------------------------------------------

def titulo(texto):
    print(f"\n{'=' * 55}")
    print(f"  {texto}")
    print('=' * 55)

def paso(n, texto):
    print(f"\n[Paso {n}] {texto}")
    print("-" * 45)

def preguntar(mensaje, default=""):
    sufijo = f" [{default}]" if default else ""
    respuesta = input(f"  {mensaje}{sufijo}: ").strip()
    return respuesta if respuesta else default

def ok(texto):
    print(f"  OK  {texto}")

def warn(texto):
    print(f"  [!] {texto}")

def error(texto):
    print(f"  [X] {texto}")

# ---------------------------------------------------------------------------
# Paso 1 - Verificar Python
# ---------------------------------------------------------------------------

def verificar_python():
    paso(1, "Verificando Python")
    version = sys.version_info
    print(f"  Python {version.major}.{version.minor}.{version.micro}")
    if version < (3, 8):
        error("Se requiere Python 3.8 o superior.")
        sys.exit(1)
    ok("Version de Python compatible.")

# ---------------------------------------------------------------------------
# Paso 2 - Instalar dependencias
# ---------------------------------------------------------------------------

DEPENDENCIAS = ["pandas", "pyodbc", "requests", "python-dotenv"]

def instalar_dependencias():
    paso(2, "Instalando dependencias de Python")
    for paquete in DEPENDENCIAS:
        print(f"  Instalando {paquete}...", end=" ", flush=True)
        resultado = subprocess.run(
            [sys.executable, "-m", "pip", "install", paquete, "--quiet"],
            capture_output=True
        )
        if resultado.returncode == 0:
            print("OK")
        else:
            print("ERROR")
            warn(resultado.stderr.decode(errors="replace"))

# ---------------------------------------------------------------------------
# Paso 3 - Detectar SFS
# ---------------------------------------------------------------------------

RUTAS_SFS_DATA = [
    r"C:\SFS_v2.1\sunat_archivos\sfs\DATA",
    r"C:\SFS_v-2.1\sunat_archivos\sfs\DATA",
]
RUTAS_SFS_RPTA = [
    r"C:\SFS_v2.1\sunat_archivos\sfs\RPTA",
    r"C:\SFS_v-2.1\sunat_archivos\sfs\RPTA",
]
RUTAS_SFS_BD = [
    r"C:\SFS_v-2.1\bd\BDFacturador.db",
    r"C:\SFS_v2.1\bd\BDFacturador.db",
]

def _detectar(rutas, nombre):
    for ruta in rutas:
        if os.path.exists(ruta):
            ok(f"{nombre}: {ruta}")
            return ruta
    warn(f"{nombre} no encontrado en las rutas por defecto.")
    return ""

def detectar_sfs():
    paso(3, "Detectando rutas del SFS")
    data = _detectar(RUTAS_SFS_DATA, "Carpeta DATA")
    rpta = _detectar(RUTAS_SFS_RPTA, "Carpeta RPTA")
    bd   = _detectar(RUTAS_SFS_BD,   "Base de datos SFS")
    return data, rpta, bd

# ---------------------------------------------------------------------------
# Paso 4 - Configurar .env
# ---------------------------------------------------------------------------

def configurar_env(sfs_data, sfs_rpta, sfs_bd):
    paso(4, "Configuracion del archivo .env")
    print("  Presiona Enter para aceptar el valor entre corchetes.\n")

    db_server   = preguntar("Servidor SQL Server",  r".\SQLEXPRESS")
    db_database = preguntar("Base de datos",        "AUXILIAR")
    sfs_url     = preguntar("URL del SFS",          "http://localhost:9000")
    sfs_data    = preguntar("Carpeta DATA del SFS", sfs_data or r"C:\SFS_v2.1\sunat_archivos\sfs\DATA")
    sfs_rpta    = preguntar("Carpeta RPTA del SFS", sfs_rpta or r"C:\SFS_v2.1\sunat_archivos\sfs\RPTA")
    sfs_bd      = preguntar("Base de datos SFS BD", sfs_bd   or r"C:\SFS_v-2.1\bd\BDFacturador.db")
    emisor_ruc  = preguntar("RUC del emisor (dejar vacío para leer de SQL Server)", "")

    contenido = f"""# ============================================================
# CONFIGURACION ROBOT SUNAT
# Generado por el instalador. Editar si cambian las rutas.
# ============================================================

# --- Base de datos SQL Server ---
DB_SERVER={db_server}
DB_DATABASE={db_database}

# --- SFS (Sistema Facturador SUNAT v2.1) ---
SFS_BASE_URL={sfs_url}
SFS_BD_PATH={sfs_bd}
SFS_DATA_DIR={sfs_data}
SFS_RPTA_DIR={sfs_rpta}

# --- Emisor (dejar vacio para leer de la tabla Emisores) ---
EMISOR_RUC={emisor_ruc}
"""
    ruta_env = os.path.join(DIRECTORIO, ".env")
    with open(ruta_env, "w", encoding="utf-8") as f:
        f.write(contenido)
    ok(f".env guardado en: {ruta_env}")

# ---------------------------------------------------------------------------
# Paso 5 - Registrar tarea programada (23:59)
# ---------------------------------------------------------------------------

def registrar_tarea():
    paso(5, "Registrando tarea en el Programador de Tareas (23:59 diario)")

    python_exe = sys.executable
    script     = os.path.join(DIRECTORIO, "sincronizar_auto.py")
    xml_path   = os.path.join(DIRECTORIO, "_tarea_sunat_temp.xml")

    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-01-01T23:59:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>"{script}"</Arguments>
      <WorkingDirectory>{DIRECTORIO}</WorkingDirectory>
    </Exec>
  </Actions>
  <Settings>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
  </Settings>
</Task>"""

    with open(xml_path, "w", encoding="utf-16") as f:
        f.write(xml)

    resultado = subprocess.run(
        ["schtasks.exe", "/Create", "/TN", "RobotSUNAT\\SincronizarDiario",
         "/XML", xml_path, "/F"],
        capture_output=True
    )

    try:
        os.remove(xml_path)
    except OSError:
        pass

    if resultado.returncode == 0:
        ok("Tarea 'RobotSUNAT\\SincronizarDiario' registrada.")
    else:
        warn("No se pudo registrar la tarea automaticamente.")
        warn("Ejecuta el instalador como Administrador para registrarla.")

# ---------------------------------------------------------------------------
# Paso 6 - Acceso directo en el escritorio
# ---------------------------------------------------------------------------

def crear_acceso_directo():
    paso(6, "Creando acceso directo en el Escritorio")

    bat_origen = os.path.join(DIRECTORIO, "iniciar_consola.bat")
    escritorio = os.path.join(os.path.expanduser("~"), "Desktop")
    bat_destino = os.path.join(escritorio, "Robot SUNAT.bat")

    if not os.path.exists(bat_origen):
        warn("No se encontró iniciar_consola.bat")
        return

    try:
        shutil.copy2(bat_origen, bat_destino)
        ok(f"Acceso directo creado en: {bat_destino}")
    except Exception as e:
        warn(f"No se pudo crear el acceso directo: {e}")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    titulo("INSTALADOR ROBOT SUNAT v1.0")
    print("\n  Este asistente configura el Robot SUNAT en esta PC.")
    print("  Tarda menos de 2 minutos.\n")
    input("  Presiona Enter para comenzar...")

    verificar_python()
    instalar_dependencias()
    sfs_data, sfs_rpta, sfs_bd = detectar_sfs()
    configurar_env(sfs_data, sfs_rpta, sfs_bd)
    registrar_tarea()
    crear_acceso_directo()

    titulo("INSTALACION COMPLETADA")
    print("""
  Proximos pasos:
    1. Asegurate de que el SFS (facturadorApp-2.1.jar) este corriendo.
    2. Abre 'Robot SUNAT.bat' en el Escritorio para usar la consola.
    3. La sincronizacion automatica se ejecutara todos los dias a las 23:59.

  Si necesitas cambiar alguna ruta o configuracion,
  edita el archivo .env en esta carpeta.
""")
    input("  Presiona Enter para cerrar...")
