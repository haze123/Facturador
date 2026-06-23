"""
Ejecutado automáticamente por el Programador de Tareas de Windows a las 23:59.
Corre la sincronización completa: genera archivos SFS, envía a SUNAT y procesa CDRs.
"""
import logging
import sys
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "sincronizar_auto.log"),
            encoding="utf-8"
        ),
    ]
)

import app_consola

if __name__ == "__main__":
    app_consola.sincronizar_facturador()
