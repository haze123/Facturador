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
        "--onefile",              # un solo archivo, sin carpeta de dependencias
        "--console",              # es un menu de consola, no una app grafica
        "--name", NOMBRE,
        "--distpath", AQUI,       # el .exe queda junto a los fuentes
        "--workpath", os.path.join(AQUI, "_build"),
        "--specpath", os.path.join(AQUI, "_build"),
        "--paths", AQUI,
        # PyInstaller no los ve porque se importan por nombre dentro del menu.
        "--hidden-import", "sistema",
        "--hidden-import", "contribuyente",
        "--hidden-import", "chequeos",
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

    exe = os.path.join(AQUI, f"{NOMBRE}.exe")
    if not os.path.exists(exe):
        print("\nNo se genero el ejecutable.")
        return 1
    print(f"\nListo: {exe}  ({os.path.getsize(exe) / 1048576:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
