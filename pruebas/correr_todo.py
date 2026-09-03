"""
Corre todas las pruebas de esta carpeta. Desde la raiz del proyecto:

    python pruebas/correr_todo.py

Devuelve 1 si alguna falla, para poder encadenarlo con otra cosa. Cada prueba
corre en su propio proceso: comparten el modulo main, y varias le reemplazan
funciones o constantes para simular la BD y el SFS.

Ninguna necesita base de datos ni el SFS corriendo. Las que tocan SQL Server
--las que probaban el adaptador contra una instancia real-- se perdieron; ver el
comentario de recuperacion en la historia del repo.
"""
import os
import subprocess
import sys

AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = os.path.dirname(AQUI)


def main():
    pruebas = sorted(
        f for f in os.listdir(AQUI)
        if f.startswith("test_") and f.endswith(".py")
    )
    if not pruebas:
        print("No hay pruebas en", AQUI)
        return 1

    fallaron = []
    for nombre in pruebas:
        r = subprocess.run(
            [sys.executable, os.path.join("pruebas", nombre)],
            capture_output=True, text=True, cwd=RAIZ,
        )
        estado = "OK  " if r.returncode == 0 else "FALLA"
        # De la salida solo interesa el veredicto; el detalle esta en el propio test.
        ultima = [l for l in r.stdout.splitlines() if l.strip()]
        print("  [%s] %-26s %s" % (estado, nombre, ultima[-1].strip() if ultima else ""))
        if r.returncode != 0:
            fallaron.append((nombre, r.stdout, r.stderr))

    print()
    if fallaron:
        for nombre, salida, error in fallaron:
            print("=" * 70)
            print(nombre)
            print("=" * 70)
            for linea in salida.splitlines():
                if "FALLA" in linea:
                    print(linea)
            if error.strip():
                print(error.strip()[-800:])
        print("\n%d de %d pruebas fallaron." % (len(fallaron), len(pruebas)))
        return 1

    print("Las %d pruebas pasaron." % len(pruebas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
