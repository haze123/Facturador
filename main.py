"""
main.py — Daemon de Facturación Electrónica SUNAT (SFS v2.1)
Gestionar con PM2: pm2 start ecosystem.config.js

Hilos:
  - Hilo Generador : cada INTERVALO_GENERACION_SEG segundos lee SQL Server,
                     genera archivos SFS y los envía al facturador local.
  - Hilo CDR       : revisa sobre carpeta RPTA, procesa CDRs al instante.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pyodbc
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

_BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE, ".env"))

# Intervalos
INTERVALO_GENERACION_SEG = int(os.getenv("INTERVALO_GENERACION_SEG", "60"))

# SQL Server
DB_CONFIG = {
    "driver":             os.getenv("DB_DRIVER",   "{SQL Server}"),
    "server":             os.getenv("DB_SERVER",   r".\SQLEXPRESS"),
    "database":           os.getenv("DB_DATABASE", "AUXILIAR"),
    "trusted_connection": os.getenv("DB_TRUSTED",  "yes"),
    "timeout":            30,
}

# Rutas SFS
SFS_DATA_DIR = p if os.path.exists(p := os.getenv("SFS_DATA_DIR", r"C:\SFS_v2.1\sunat_archivos\sfs\DATA")) else os.path.join(_BASE, "sunat_archivos", "DATA")
SFS_RPTA_DIR = p if os.path.exists(p := os.getenv("SFS_RPTA_DIR", r"C:\SFS_v2.1\sunat_archivos\sfs\RPTA")) else os.path.join(_BASE, "sunat_archivos", "RPTA")

SFS_BD_PATH  = os.getenv("SFS_BD_PATH",  r"C:\SFS_v2.1\bd\BDFacturador.db")
SFS_BASE_URL = os.getenv("SFS_BASE_URL", "http://localhost:9000")

DIR_PROCESADOS = os.path.join(SFS_RPTA_DIR, "procesados")
DIR_ERRORES    = os.path.join(SFS_RPTA_DIR, "errores")

# Emisor override (opcional)
EMISOR_RUC_OVERRIDE = os.getenv("EMISOR_RUC", "").strip()

# Estados SFS que se reintentan
_ESTADOS_ERROR = ("05", "10")
# Tipos soportados por SFS
_TIPOS_SFS = {"01", "03", "07", "08", "RC", "RA"}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(_BASE, "facturador.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------


def formatear_decimal(valor) -> Decimal:
    if valor is None:
        return Decimal("0.00")
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def formatear_fecha_hora(fecha_raw) -> datetime:
    if isinstance(fecha_raw, datetime):
        return fecha_raw
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(fecha_raw).strip(), fmt)
        except (ValueError, AttributeError):
            pass
    raise ValueError(f"Fecha inválida: {fecha_raw!r}")


def escribir_archivo(ruta: str, contenido: str):
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(contenido)
    os.replace(tmp, ruta)


def _mover(ruta: str, carpeta: str):
    os.makedirs(carpeta, exist_ok=True)
    try:
        os.replace(ruta, os.path.join(carpeta, os.path.basename(ruta)))
    except Exception:
        logger.exception("No se pudo mover %s a %s", ruta, carpeta)

# ---------------------------------------------------------------------------
# Base de datos — SQL Server
# ---------------------------------------------------------------------------

def conectar_bd():
    conn_str = (
        f"DRIVER={DB_CONFIG['driver']};SERVER={DB_CONFIG['server']};"
        f"DATABASE={DB_CONFIG['database']};Trusted_Connection={DB_CONFIG['trusted_connection']};"
    )
    return pyodbc.connect(conn_str, timeout=DB_CONFIG["timeout"])


def _fila_dict(cur, fila):
    return dict(zip([d[0] for d in cur.description], fila))


def obtener_emisor(conn):
    cur = conn.cursor()
    cur.execute("SELECT TOP 1 ruc, razon_social FROM Emisores")
    fila = cur.fetchone()
    return _fila_dict(cur, fila) if fila else None


def obtener_receptor(conn, receptor_id):
    if receptor_id is None:
        return {}
    cur = conn.cursor()
    cur.execute("SELECT TOP 1 * FROM Receptores WHERE id = ?", (int(receptor_id),))
    fila = cur.fetchone()
    return _fila_dict(cur, fila) if fila else {}


def obtener_items(conn, comprobante_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM Items WHERE ComprobanteId = ?", (int(comprobante_id),))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, fila)) for fila in cur.fetchall()]


def obtener_comprobantes_pendientes(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM Comprobantes WHERE enviado IS NULL OR enviado = 0 ORDER BY fecha_emision ASC")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, fila)) for fila in cur.fetchall()]

# ---------------------------------------------------------------------------
# Generador de archivos SFS
# ---------------------------------------------------------------------------

def _nombre_base(ruc: str, tipo: str, num: str) -> str:
    serie, corr = num.split("-", 1) if "-" in num else ("0000", num or "00000000")
    nombre = f"{ruc}-{tipo}-{serie}-{corr}"
    return re.sub(r'[<>:"/\\|?*\n\r\t]+', "_", nombre.strip())[:250]


def procesar_comprobante(conn, comp: dict, ruc_emisor: str) -> bool:
    comp_id   = comp["id"]
    num_comp  = str(comp.get("numeracion_comprobante", "")).strip()
    tipo_comp = str(comp.get("tipo_comprobante", "")).strip() or "01"

    base  = _nombre_base(ruc_emisor, tipo_comp, num_comp)
    os.makedirs(SFS_DATA_DIR, exist_ok=True)
    rutas = {e: os.path.join(SFS_DATA_DIR, f"{base}.{e}") for e in ("cab", "det", "tri", "ley", "PAG")}

    enviado = comp.get("enviado")
    faltan  = any(not os.path.exists(rutas[e]) for e in ("cab", "det", "tri", "ley"))
    if not (enviado is None or enviado == 0 or faltan):
        return False

    receptor     = obtener_receptor(conn, comp.get("ReceptorId"))
    tipo_doc_rec = str(receptor.get("tipo_documento",   "0")).strip() or "0"
    num_doc_rec  = str(receptor.get("numero_documento", "00000000")).strip() or "00000000"
    razon_social = str(receptor.get("razon_social", "CLIENTE VARIOS")).strip() or "CLIENTE VARIOS"
    moneda       = str(comp.get("tipo_moneda", "PEN")).strip() or "PEN"

    fecha_dt  = formatear_fecha_hora(comp.get("fecha_emision"))
    fecha_str = fecha_dt.strftime("%Y-%m-%d")
    hora_str  = fecha_dt.strftime("%H:%M:%S")

    tot_grav  = formatear_decimal(comp.get("gravadas")  or comp.get("total_gravadas"))
    tot_igv   = formatear_decimal(comp.get("igv")       or comp.get("total_igv"))
    tot_venta = formatear_decimal(comp.get("total")     or comp.get("total_venta"))

    items      = obtener_items(conn, comp_id)
    lineas_det = []

    for item in items:
        cant   = formatear_decimal(item.get("dec_cantidad") or item.get("cantidad_venta") or item.get("cantidad", 1))
        desc   = str(item.get("descripcion", "ITEM")).replace("|", " ").strip() or "ITEM"
        codigo = str(item.get("codigo_producto", "-")).strip() or "-"
        medida = str(item.get("medida", "NIU")).strip() or "NIU"
        v_unit = formatear_decimal(item.get("valor"))
        v_vta  = formatear_decimal(item.get("valor_venta"))
        igv_it = formatear_decimal(item.get("igv_venta"))
        p_unit = formatear_decimal(item.get("precio"))

        lineas_det.append(
            f"{medida}|{cant:.2f}|{codigo}|-|{desc}|{v_unit:.6f}|"
            f"{igv_it:.2f}|1000|{igv_it:.2f}|{v_vta:.2f}|IGV|VAT|10|18.00|"
            "-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|"
            f"{p_unit:.2f}|{v_vta:.2f}|0.00|\n"
        )

    if not lineas_det:
        logger.warning("Comprobante %s sin ítems.", num_comp)

    monto_letras = str(comp.get("monto_letras") or "SIN DESCRIPCION").strip()
    if not monto_letras:
        monto_letras = "SIN DESCRIPCION"

    escribir_archivo(rutas["cab"],
        f"0101|{fecha_str}|{hora_str}|-|0000|{tipo_doc_rec}|{num_doc_rec}|"
        f"{razon_social}|{moneda}|{tot_igv:.2f}|{tot_grav:.2f}|{tot_venta:.2f}|"
        f"0.00|0.00|0.00|{tot_venta:.2f}|2.1|2.0|\n"
    )
    escribir_archivo(rutas["PAG"], f"Contado|{tot_venta:.2f}|{moneda}|\n")
    escribir_archivo(rutas["tri"], f"1000|IGV|VAT|{tot_grav:.2f}|{tot_igv:.2f}|\n")
    escribir_archivo(rutas["ley"], f"1000|{monto_letras}|\n")

    if lineas_det:
        escribir_archivo(rutas["det"], "".join(lineas_det))
    elif os.path.exists(rutas["det"]):
        os.remove(rutas["det"])

    conn.cursor().execute("UPDATE Comprobantes SET enviado = 1 WHERE id = ?", (int(comp_id),))
    conn.commit()
    logger.info("Archivos SFS generados: %s", num_comp)
    return True

# ---------------------------------------------------------------------------
# API REST del SFS local
# ---------------------------------------------------------------------------

def _sfs_post(path: str, payload: dict):
    url = f"{SFS_BASE_URL}/{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.URLError as e:
        logger.warning("SFS no disponible (%s): %s", url, e)
        return None
    except Exception:
        logger.exception("Error llamando SFS %s", path)
        return None


def activar_procesamiento_sfs(documentos: list):
    if not documentos:
        return
    try:
        urllib.request.urlopen(f"{SFS_BASE_URL}/", timeout=3)
    except Exception:
        logger.warning("SFS no responde — envío automático desactivado.")
        return

    for doc in documentos:
        tip   = str(doc.get("tip_docu", "")).strip()
        label = f"{tip}-{doc['num_docu']}"
        if tip not in _TIPOS_SFS:
            logger.info("[SFS] Tipo %s no soportado, omitido: %s", tip, label)
            continue
        payload = {k: doc[k] for k in ("num_ruc", "tip_docu", "num_docu")}

        r1 = _sfs_post("api/GenerarComprobante.htm", payload)
        if not (r1 and r1.get("validacion") == "EXITO"):
            logger.warning("[SFS] Error al generar XML para %s: %s", label, r1)
            continue

        time.sleep(2)
        r2 = _sfs_post("api/enviarXML.htm", payload)
        if r2 and r2.get("validacion") == "EXITO":
            logger.info("[SFS] Enviado a SUNAT: %s", label)
        else:
            time.sleep(3)
            r2 = _sfs_post("api/enviarXML.htm", payload)
            if r2 and r2.get("validacion") == "EXITO":
                logger.info("[SFS] Enviado a SUNAT (reintento): %s", label)
            else:
                logger.warning("[SFS] Error al enviar %s: %s", label, r2)
        time.sleep(1)

# ---------------------------------------------------------------------------
# SFS BD SQLite — gestión de estados
# ---------------------------------------------------------------------------

def _registrar_en_sfs_bd(ruc_emisor: str, docs: list):
    if not os.path.exists(SFS_BD_PATH):
        return
    time.sleep(2)
    with sqlite3.connect(SFS_BD_PATH) as sfs:
        for doc in docs:
            tip = str(doc.get("tip_docu", "")).strip()
            if tip not in _TIPOS_SFS:
                continue
            num  = str(doc.get("num_docu", "")).strip()
            arch = f"{ruc_emisor}-{tip}-{num}"
            existe = sfs.execute(
                "SELECT 1 FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                (ruc_emisor, tip, num),
            ).fetchone()
            if not existe:
                sfs.execute(
                    "INSERT INTO DOCUMENTO (NUM_RUC, TIP_DOCU, NUM_DOCU, NOM_ARCH, IND_SITU, DES_OBSE) "
                    "VALUES (?,?,?,?,?,?)",
                    (ruc_emisor, tip, num, arch, "03", "Aceptado por SUNAT"),
                )


def _tiene_cdr(ruc: str, tip: str, num: str) -> bool:
    nombre = f"R{ruc}-{tip}-{num}.zip"
    return any(os.path.exists(os.path.join(d, nombre)) for d in (SFS_RPTA_DIR, DIR_PROCESADOS))


def _eliminar_data_files(nom_arch: str):
    for ext in ("cab", "det", "tri", "ley", "PAG", "RDI", "TRD", "DET"):
        ruta = os.path.join(SFS_DATA_DIR, f"{nom_arch}.{ext}")
        if os.path.exists(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass


def _activar_pendientes_sfs_bd(ruc_emisor: str, ya_procesados: list):
    if not os.path.exists(SFS_BD_PATH):
        return
    ya_keys = {(d["tip_docu"], d["num_docu"]) for d in ya_procesados}
    with sqlite3.connect(SFS_BD_PATH) as sfs:
        rows = sfs.execute(
            "SELECT TIP_DOCU, NUM_DOCU, NOM_ARCH FROM DOCUMENTO "
            "WHERE NUM_RUC=? AND TIP_DOCU IN ('01','03','07','08') AND IND_SITU IN ('01','02')",
            (ruc_emisor,),
        ).fetchall()
        docs_extra = []
        for tip, num, nom_arch in rows:
            if (tip, num) in ya_keys:
                continue
            if _tiene_cdr(ruc_emisor, tip, num):
                sfs.execute(
                    "UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE='Aceptado (CDR procesado)' "
                    "WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? AND IND_SITU IN ('01','02')",
                    (ruc_emisor, tip, num),
                )
                _eliminar_data_files(nom_arch or f"{ruc_emisor}-{tip}-{num}")
                continue
            docs_extra.append({"num_ruc": ruc_emisor, "tip_docu": tip, "num_docu": num})
    if docs_extra:
        logger.info("%d doc(s) en SFS BD pendientes de activar.", len(docs_extra))
        activar_procesamiento_sfs(docs_extra)


def resetear_rechazados(conn, ruc_emisor: str):
    if not os.path.exists(SFS_BD_PATH):
        return
    with sqlite3.connect(SFS_BD_PATH) as sfs:
        rows = sfs.execute(
            "SELECT NUM_DOCU, TIP_DOCU FROM DOCUMENTO WHERE NUM_RUC=? AND IND_SITU IN (?,?)",
            (ruc_emisor, *_ESTADOS_ERROR),
        ).fetchall()
        if not rows:
            return
        cur = conn.cursor()
        for num_docu, tip_docu in rows:
            cur.execute(
                "UPDATE Comprobantes SET enviado=0 WHERE numeracion_comprobante=? AND tipo_comprobante=?",
                (num_docu, tip_docu),
            )
        sfs.execute(
            "DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND IND_SITU IN (?,?)",
            (ruc_emisor, *_ESTADOS_ERROR),
        )
    conn.commit()
    logger.info("Reseteados %d comprobantes rechazados para reenvío.", len(rows))

# ---------------------------------------------------------------------------
# Parser CDR — respuestas SUNAT
# ---------------------------------------------------------------------------

def _iter_elementos(elem, ancs=()):
    yield elem, ancs
    for hijo in elem:
        yield from _iter_elementos(hijo, ancs + (elem.tag.split("}")[-1],))


def _extraer_numeracion(texto) -> str | None:
    m = re.search(r"[A-Z]{1,3}\d{3}-\d+", str(texto or ""))
    return m.group(0) if m else None


def parsear_xml_cdr(fuente) -> dict:
    res = {"numeracion": None, "codigo": None, "descripcion": None, "status": "PENDIENTE"}
    try:
        root = ET.fromstring(fuente) if isinstance(fuente, bytes) else ET.parse(fuente).getroot()
        descripciones = []
        for elem, ancs in _iter_elementos(root):
            if not isinstance(elem.tag, str):
                continue
            tag  = elem.tag.split("}")[-1].lower()
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
                if isinstance(elem.tag, str) and elem.tag.split("}")[-1].lower() == "description":
                    if (elem.text or "").strip():
                        descripciones.append((elem.text or "").strip())
                        break

        res["descripcion"] = " | ".join(dict.fromkeys(descripciones)) if descripciones else None

        if not res["numeracion"]:
            res["numeracion"] = _extraer_numeracion(res["descripcion"])
        if not res["numeracion"] and isinstance(fuente, str):
            res["numeracion"] = _extraer_numeracion(os.path.basename(fuente))

        codigo = (res["codigo"]      or "").lower()
        desc   = (res["descripcion"] or "").lower()
        if "acept" in desc or codigo in {"0", "01", "0000"}:
            res["status"] = "ACEPTADO"
        elif "rechaz" in desc or "error" in desc or "no autorizado" in desc:
            res["status"] = "RECHAZADO"
        elif "observ" in desc:
            res["status"] = "OBSERVADO"

    except Exception:
        logger.exception("Error parseando CDR %s", fuente if isinstance(fuente, str) else "<bytes>")
        res["status"] = "ERROR"
    return res


def _actualizar_sql_cdr(conn, numeracion: str, parsed: dict) -> bool:
    if not numeracion or parsed["status"] != "ACEPTADO":
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE Comprobantes SET enviado = 1 WHERE numeracion_comprobante = ?",
            (numeracion,),
        )
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        logger.exception("Error actualizando SQL para %s", numeracion)
        return False


def procesar_respuestas():
    if not os.path.exists(SFS_RPTA_DIR):
        return

    archivos = [
        os.path.join(SFS_RPTA_DIR, f)
        for f in os.listdir(SFS_RPTA_DIR)
        if f.lower().endswith((".zip", ".xml"))
    ]
    if not archivos:
        return

    conn = None
    try:
        conn = conectar_bd()
        ok = err = 0
        for ruta in archivos:
            nombre = os.path.basename(ruta)
            try:
                if ruta.lower().endswith(".zip"):
                    with zipfile.ZipFile(ruta) as z:
                        xml_names = [n for n in z.namelist() if n.lower().endswith(".xml")]
                        if not xml_names:
                            _mover(ruta, DIR_ERRORES); err += 1; continue
                        parsed = parsear_xml_cdr(z.read(xml_names[0]))
                    if not parsed["numeracion"]:
                        parsed["numeracion"] = _extraer_numeracion(nombre)
                else:
                    parsed = parsear_xml_cdr(ruta)

                num = parsed["numeracion"]
                logger.info("CDR %s | %s [%s]", nombre, num, parsed["status"])

                if _actualizar_sql_cdr(conn, num, parsed):
                    ok += 1
                _mover(ruta, DIR_PROCESADOS)

            except zipfile.BadZipFile:
                logger.warning("ZIP corrupto: %s", nombre)
                _mover(ruta, DIR_ERRORES); err += 1
            except Exception:
                logger.exception("Error procesando CDR %s", nombre)
                err += 1

        if ok or err:
            logger.info("CDRs procesados — OK: %d | Errores: %d", ok, err)
    finally:
        if conn:
            conn.close()

# ---------------------------------------------------------------------------
# Flujo completo de generación
# ---------------------------------------------------------------------------

def ciclo_generacion():
    logger.info("Consultando BD...")
    conn = None
    try:
        conn = conectar_bd()
        emisor = obtener_emisor(conn)
        if not emisor:
            logger.error("No se encontró información del Emisor en BD.")
            return

        ruc_emisor = EMISOR_RUC_OVERRIDE or str(emisor.get("ruc", "")).strip() or "00000000000"

        resetear_rechazados(conn, ruc_emisor)

        comprobantes = obtener_comprobantes_pendientes(conn)
        logger.info("%d comprobante(s) pendiente(s).", len(comprobantes))
        docs_generados = []
        for comp in comprobantes:
            try:
                if procesar_comprobante(conn, comp, ruc_emisor):
                    docs_generados.append({
                        "num_ruc":  ruc_emisor,
                        "tip_docu": str(comp.get("tipo_comprobante", "")).strip().zfill(2),
                        "num_docu": str(comp.get("numeracion_comprobante", "")).strip(),
                    })
            except Exception:
                logger.exception("Error procesando %r", comp.get("numeracion_comprobante"))

        if docs_generados:
            logger.info("%d comprobante(s) generados, enviando al SFS...", len(docs_generados))
            activar_procesamiento_sfs(docs_generados)
            _registrar_en_sfs_bd(ruc_emisor, docs_generados)

        _activar_pendientes_sfs_bd(ruc_emisor, docs_generados)

    except Exception:
        logger.exception("Error en ciclo_generacion")
    finally:
        if conn:
            conn.close()

# ---------------------------------------------------------------------------
# Hilo 1 — Generador (loop cada N segundos)
# ---------------------------------------------------------------------------

def hilo_generador():
    logger.info("Hilo GENERADOR iniciado (intervalo: %ds)", INTERVALO_GENERACION_SEG)
    while True:
        ciclo_generacion()
        time.sleep(INTERVALO_GENERACION_SEG)

# ---------------------------------------------------------------------------
# Hilo 2 — CDR (reacciona al instante cuando llega un ZIP)
# ---------------------------------------------------------------------------

class CDRHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        if event.src_path.lower().endswith((".zip", ".xml")):
            logger.info("CDR detectado: %s", os.path.basename(event.src_path))
            time.sleep(1)
            procesar_respuestas()


def hilo_cdr():
    logger.info("Hilo CDR iniciado — monitoreando: %s", SFS_RPTA_DIR)
    os.makedirs(SFS_RPTA_DIR, exist_ok=True)
    os.makedirs(DIR_PROCESADOS, exist_ok=True)
    os.makedirs(DIR_ERRORES,    exist_ok=True)

    handler  = CDRHandler()
    observer = Observer()
    observer.schedule(handler, path=SFS_RPTA_DIR, recursive=False)
    observer.start()

    try:
        while True:
            time.sleep(10)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

# ---------------------------------------------------------------------------
# Main — lanza ambos hilos
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  FACTURADOR SUNAT - v2.0")
    logger.info("  SFS DATA : %s", SFS_DATA_DIR)
    logger.info("  SFS RPTA : %s", SFS_RPTA_DIR)
    logger.info("  SQL SERVER: %s / %s", DB_CONFIG["server"], DB_CONFIG["database"])
    logger.info("=" * 60)

    procesar_respuestas()

    t_generador = threading.Thread(target=hilo_generador, name="Generador", daemon=True)
    t_cdr       = threading.Thread(target=hilo_cdr,       name="CDR",       daemon=True)

    t_generador.start()
    t_cdr.start()

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Deteniendo...")

