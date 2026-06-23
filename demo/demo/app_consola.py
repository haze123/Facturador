import logging
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from urllib.parse import urlparse

import pandas as pd
import pyodbc
import requests

import generador_sfs

BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_DESCARGAS = os.path.join(BASE_DIR, "comprobantes_descargados")

_SFS_RPTA    = r"C:\SFS_v2.1\sunat_archivos\sfs\RPTA"
RESPUESTAS_DIR = (
    os.getenv("FACTURADOR_SUNAT_RESPONSE_DIR", "").strip()
    or (_SFS_RPTA if os.path.exists(_SFS_RPTA)
        else os.path.join(BASE_DIR, "sunat_archivos", "RESPUESTAS"))
)
DIR_PROCESADOS = os.path.join(RESPUESTAS_DIR, "procesados")
DIR_ERRORES    = os.path.join(RESPUESTAS_DIR, "errores")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def conectar_bd():
    cfg = generador_sfs.DB_CONFIG
    try:
        return pyodbc.connect(
            f"DRIVER={cfg['driver']};SERVER={cfg['server']};"
            f"DATABASE={cfg['database']};Trusted_Connection={cfg['trusted_connection']};",
            timeout=cfg['timeout']
        )
    except pyodbc.Error:
        logger.exception("Error conectando a SQL Server")
        return None


# ---------------------------------------------------------------------------
# Parseo de CDR XML (respuestas SUNAT)
# ---------------------------------------------------------------------------

def _iter_elementos(elem, ancs=()):
    yield elem, ancs
    for hijo in elem:
        yield from _iter_elementos(hijo, ancs + (elem.tag.split('}')[-1],))


def _extraer_numeracion(texto):
    m = re.search(r"[A-Z]{1,3}\d{3}-\d+", str(texto or ""))
    return m.group(0) if m else None


def parsear_xml_cdr(ruta_xml):
    res = {"numeracion": None, "codigo": None, "descripcion": None, "status": "PENDIENTE"}
    try:
        root        = ET.parse(ruta_xml).getroot()
        descripciones = []
        for elem, ancs in _iter_elementos(root):
            if not isinstance(elem.tag, str):
                continue
            tag  = elem.tag.split('}')[-1].lower()
            text = (elem.text or "").strip()
            if not text:
                continue
            if tag == "responsecode" and not res["codigo"]:
                res["codigo"] = text
            elif tag in {"description", "responsedescription"}:
                if any("response" in a.lower() for a in ancs):
                    descripciones.append(text)
            elif tag == "referenceid" and not res["numeracion"]:
                res["numeracion"] = _extraer_numeracion(text)
            elif tag == "id" and not res["numeracion"]:
                res["numeracion"] = _extraer_numeracion(text)

        if not descripciones:
            for elem, _ in _iter_elementos(root):
                if isinstance(elem.tag, str) and elem.tag.split('}')[-1].lower() == "description":
                    if (elem.text or "").strip():
                        descripciones.append((elem.text or "").strip())
                        break

        res["descripcion"] = " | ".join(dict.fromkeys(descripciones)) if descripciones else None

        if not res["numeracion"]:
            res["numeracion"] = _extraer_numeracion(res["descripcion"])
        if not res["numeracion"]:
            res["numeracion"] = _extraer_numeracion(os.path.basename(ruta_xml))

        codigo = (res["codigo"]      or "").lower()
        desc   = (res["descripcion"] or "").lower()
        if "acept" in desc or codigo in {"0", "01", "0000"}:
            res["status"] = "ACEPTADO"
        elif "rechaz" in desc or "error" in desc or "no autorizado" in desc:
            res["status"] = "RECHAZADO"
        elif "observ" in desc:
            res["status"] = "OBSERVADO"

    except Exception:
        logger.exception("Error parseando CDR %s", ruta_xml)
        res["status"] = "ERROR"
    return res


def _mover(ruta, carpeta):
    os.makedirs(carpeta, exist_ok=True)
    try:
        os.replace(ruta, os.path.join(carpeta, os.path.basename(ruta)))
    except Exception:
        logger.exception("No se pudo mover %s a %s", ruta, carpeta)


def _actualizar_sql(conn, numeracion, parsed, origen):
    if not numeracion:
        return False
    try:
        df = pd.read_sql(
            "SELECT TOP 1 * FROM Comprobantes WHERE numeracion_comprobante = ?",
            conn, params=(numeracion,)
        )
        if df.empty:
            return False
        cols = list(df.columns)
        upd, params = [], []
        for col, val in [
            ("estado",               parsed["status"]),
            ("respuesta_codigo",     parsed["codigo"]),
            ("respuesta_descripcion",parsed["descripcion"]),
            ("respuesta_xml",        origen),
        ]:
            if col in cols:
                upd.append(f"{col} = ?"); params.append(val)
        if "enviado" in cols and parsed["status"] == "ACEPTADO":
            upd.append("enviado = ?"); params.append(1)
        if not upd:
            return False
        conn.cursor().execute(
            f"UPDATE Comprobantes SET {', '.join(upd)} WHERE numeracion_comprobante = ?",
            params + [numeracion]
        )
        conn.commit()
        return True
    except Exception:
        logger.exception("Error actualizando SQL Server para %s", numeracion)
        return False


# ---------------------------------------------------------------------------
# Procesamiento de respuestas SUNAT
# ---------------------------------------------------------------------------

def procesar_respuestas():
    """Procesa CDRs de SUNAT: archivos .zip y .xml en la carpeta RPTA."""
    print(f"\n--- PROCESANDO RESPUESTAS SUNAT ({RESPUESTAS_DIR}) ---")
    if not os.path.exists(RESPUESTAS_DIR):
        print("[!] Carpeta RPTA no encontrada.")
        return

    archivos = [
        os.path.join(RESPUESTAS_DIR, f)
        for f in os.listdir(RESPUESTAS_DIR)
        if f.lower().endswith(('.zip', '.xml'))
    ]
    if not archivos:
        print("[!] Sin CDRs pendientes.")
        return

    conn = conectar_bd()
    if not conn:
        return

    ok = err = 0
    for ruta in archivos:
        nombre = os.path.basename(ruta)
        try:
            if ruta.lower().endswith('.zip'):
                with zipfile.ZipFile(ruta) as z:
                    xml_names = [n for n in z.namelist() if n.lower().endswith('.xml')]
                    if not xml_names:
                        _mover(ruta, DIR_ERRORES); err += 1; continue
                    xml_bytes = z.read(xml_names[0])
                with tempfile.NamedTemporaryFile(suffix='.xml', delete=False) as tmp:
                    tmp.write(xml_bytes)
                    tmp_path = tmp.name
                try:
                    parsed = parsear_xml_cdr(tmp_path)
                finally:
                    try: os.unlink(tmp_path)
                    except OSError: pass
                if not parsed["numeracion"]:
                    parsed["numeracion"] = _extraer_numeracion(nombre)
            else:
                parsed = parsear_xml_cdr(ruta)

            num = parsed["numeracion"]
            print(f"  {nombre}: {num} -> {parsed['status']}")
            if _actualizar_sql(conn, num, parsed, nombre):
                ok += 1
            _mover(ruta, DIR_PROCESADOS)

        except zipfile.BadZipFile:
            logger.warning("ZIP corrupto: %s", nombre)
            _mover(ruta, DIR_ERRORES); err += 1
        except Exception:
            logger.exception("Error procesando %s", nombre)
            err += 1

    conn.close()
    print(f"[!] OK: {ok} | Errores: {err}")


def _esperar_respuestas(timeout=90, intervalo=5):
    print(f"[i] Esperando CDRs en {RESPUESTAS_DIR}...")
    fin = time.time() + timeout
    while time.time() < fin:
        if os.path.exists(RESPUESTAS_DIR) and any(
            f.lower().endswith(('.zip', '.xml')) for f in os.listdir(RESPUESTAS_DIR)
        ):
            print("[i] CDRs detectados.")
            return True
        time.sleep(intervalo)
        print("[i] Esperando...")
    print("[!] Tiempo de espera agotado.")
    return False


# ---------------------------------------------------------------------------
# Comprobantes
# ---------------------------------------------------------------------------

def leer_comprobantes(only_pending=False):
    print("\n--- COMPROBANTES ---")
    conn = conectar_bd()
    if not conn:
        return None
    try:
        filtro = "WHERE enviado IS NULL OR enviado = 0" if only_pending else ""
        df = pd.read_sql(f"SELECT * FROM Comprobantes {filtro} ORDER BY fecha_emision DESC", conn)
        print(f"[!] {len(df)} comprobante(s) encontrados.")
        if not df.empty:
            pd.set_option('display.max_columns', None)
            pd.set_option('display.width', 1000)
            cols = [c for c in ('id','tipo_comprobante','numeracion_comprobante','fecha_emision','total_venta','enviado') if c in df.columns]
            print(df[cols or list(df.columns)].head(20))
        return df
    except Exception:
        logger.exception("Error leyendo comprobantes")
        return None
    finally:
        conn.close()


def descargar_pdfs(df):
    if df is None or df.empty or 'url' not in df.columns:
        print("[!] Sin URLs para descargar.")
        return
    os.makedirs(ARCHIVO_DESCARGAS, exist_ok=True)
    df_url = df[df['url'].notna() & (df['url'].astype(str).str.strip() != '')].copy()
    ok = err = 0
    session = requests.Session()

    for _, row in df_url.iterrows():
        url = str(row['url']).strip()
        try:
            p = urlparse(url)
            if p.scheme not in ('http', 'https') or not p.netloc:
                raise ValueError("URL inválida")
        except Exception:
            print(f"[!] URL inválida: {url}"); err += 1; continue

        fecha = row.get('fecha_emision')
        if isinstance(fecha, datetime):
            fecha_str = fecha.strftime("%Y%m%d")
        elif isinstance(fecha, str):
            fecha_str = fecha.strip()[:10].replace('-', '')
        else:
            fecha_str = "sinFecha"

        nombre = generador_sfs.limpiar_nombre_archivo(
            f"{row.get('numeracion_comprobante', 'sin-num')}_{fecha_str}.pdf"
        )
        ruta   = os.path.join(ARCHIVO_DESCARGAS, nombre)
        ruta_t = ruta + '.tmp'
        print(f"  Descargando {nombre}...", end=" ", flush=True)
        try:
            r = session.get(url, timeout=15)
            r.raise_for_status()
            ct = r.headers.get('Content-Type', '').lower()
            if 'pdf' not in ct and not r.content.startswith(b'%PDF'):
                print("No es PDF"); err += 1; continue
            with open(ruta_t, 'wb') as f:
                f.write(r.content)
            os.replace(ruta_t, ruta)
            print("OK"); ok += 1
        except Exception as e:
            print(f"Error: {e}"); err += 1
            if os.path.exists(ruta_t):
                try: os.remove(ruta_t)
                except OSError: pass

    print(f"[!] Descargados: {ok} | Errores: {err}")


# ---------------------------------------------------------------------------
# Sincronización
# ---------------------------------------------------------------------------

def sincronizar_facturador():
    print("\n--- SINCRONIZANDO CON FACTURADOR SUNAT ---")
    procesar_respuestas()
    generador_sfs.generar_archivos_sfs(only_pending=True)
    if _esperar_respuestas():
        procesar_respuestas()
    else:
        print("[!] Sin nuevos CDRs; revisa la carpeta RPTA.")


# ---------------------------------------------------------------------------
# Menú
# ---------------------------------------------------------------------------

def menu():
    opciones = {
        '1': ("Leer comprobantes",               lambda: leer_comprobantes()),
        '2': ("Leer y descargar PDFs",           lambda: descargar_pdfs(leer_comprobantes())),
        '3': ("Generar archivos SFS pendientes", lambda: generador_sfs.generar_archivos_sfs(only_pending=True)),
        '4': ("Procesar CDRs de SUNAT",          procesar_respuestas),
        '5': ("Sincronizar (generar + procesar)",sincronizar_facturador),
    }
    while True:
        print("\n" + "=" * 40)
        print("   ROBOT SUNAT - MODO CONSOLA V1.0")
        print("=" * 40)
        for k, (desc, _) in opciones.items():
            print(f"{k}. {desc}")
        print("6. Salir")
        print("=" * 40)
        opcion = input("Elige una opción (1-6): ").strip()
        if opcion == '6':
            print("Saliendo..."); break
        if opcion in opciones:
            opciones[opcion][1]()
            input("\nPresiona Enter para continuar...")
        else:
            print("[X] Opción no válida.")


if __name__ == "__main__":
    try:
        menu()
    except Exception:
        logger.exception("Error inesperado")
        input("Presiona Enter para salir...")

