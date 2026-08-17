"""
Compila el instalador a un unico .exe.

    python construir.py

Deja FacturadorSetup.exe en esta carpeta. El ejecutable lleva adentro el
interprete de Python, asi que corre en una PC donde todavia no hay Python — que
es justamente lo que el instalador va a instalar.

OJO con SmartScreen: Windows va a mostrar "Windows protegio su PC" en cada
maquina nueva, porque el ejecutable no esta firmado. Se sortea con
"Mas informacion" -> "Ejecutar de todas formas". Evitarlo requiere un
certificado de firma de codigo (entre 200 y 400 dolares al ano).
"""
import os
import shutil
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
NOMBRE = "FacturadorSetup"


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Falta PyInstaller. Instalarlo con:  python -m pip install pyinstaller")
        return 1

    comando = [
        sys.executable, "-m", "PyInstaller",
        # --onedir y no --onefile: el segundo mete todo comprimido en un unico .exe
        # que al arrancar SE DESCOMPRIME SOLO en una carpeta temporal y corre desde
        # ahi. Esa es la misma tecnica que usan los empaquetadores de malware, y los
        # antivirus la marcan por heuristica sin mirar que hace el programa. Con
        # --onedir el .exe queda con sus DLLs al lado y ese comportamiento desaparece.
        # El costo es que se distribuye una carpeta en vez de un archivo suelto.
        "--onedir",
        "--console",              # es un menu de consola, no una app grafica
        "--name", NOMBRE,
        "--distpath", AQUI,       # el .exe queda junto a los fuentes
        "--workpath", os.path.join(AQUI, "_build"),
        "--specpath", os.path.join(AQUI, "_build"),
        "--paths", AQUI,
        # La raiz del proyecto, para que entre el paquete repositorio/ del daemon:
        # el instalador prueba la conexion con el mismo codigo que despues conecta
        # de verdad, asi no puede decir que funciona y que al daemon le falle.
        "--paths", os.path.dirname(AQUI),
        # PyInstaller no los ve porque se importan por nombre dentro del menu.
        "--hidden-import", "sistema",
        "--hidden-import", "contribuyente",
        "--hidden-import", "chequeos",
        "--hidden-import", "perfil",
        # Los adaptadores se eligen en tiempo de ejecucion segun DATABASE_URL, asi
        # que hay que nombrarlos: si falta uno, ese motor no se puede ni probar.
        "--hidden-import", "repositorio",
        "--hidden-import", "repositorio.postgres",
        "--hidden-import", "repositorio.sqlserver",
        "--noconfirm",
        os.path.join(AQUI, "instalador.py"),
    ]
    print("Compilando...\n")
    resultado = subprocess.run(comando)
    if resultado.returncode != 0:
        print("\nLa compilacion fallo.")
        return resultado.returncode

    # Restos del proceso de compilacion.
    shutil.rmtree(os.path.join(AQUI, "_build"), ignore_errors=True)

    # Con --onedir el ejecutable queda dentro de su propia carpeta, junto a las DLLs.
    carpeta = os.path.join(AQUI, NOMBRE)
    exe = os.path.join(carpeta, f"{NOMBRE}.exe")
    if not os.path.exists(exe):
        print("\nNo se genero el ejecutable.")
        return 1

    # Se distribuye comprimido: son cientos de archivos y bajarlos sueltos de una
    # release no es practico.
    zip_base = os.path.join(AQUI, NOMBRE)
    if os.path.exists(zip_base + ".zip"):
        os.remove(zip_base + ".zip")
    shutil.make_archive(zip_base, "zip", AQUI, NOMBRE)

    archivos = sum(len(fs) for _, _, fs in os.walk(carpeta))
    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _, fs in os.walk(carpeta) for f in fs)
    print(f"\nListo: {carpeta}")
    print(f"       {total / 1048576:.1f} MB en {archivos} archivos")
    print(f"       para distribuir: {zip_base}.zip "
          f"({os.path.getsize(zip_base + '.zip') / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
