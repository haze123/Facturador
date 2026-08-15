"""
main.py — Daemon de Facturación Electrónica SUNAT (SFS v2.1)
Gestionar con PM2: pm2 start sfs.config.js --only facturador

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
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
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
# Red de seguridad del hilo CDR: barrido completo de RPTA además de los eventos de
# watchdog (ver hilo_cdr).
INTERVALO_BARRIDO_RPTA_SEG = int(os.getenv("INTERVALO_BARRIDO_RPTA_SEG", "30"))

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

# IND_SITU de la BD del SFS (sistema/facturador/util/Constantes del facturadorApp).
_NOMBRE_SITU = {
    "01": "por generar XML",
    "02": "XML generado",
    "03": "aceptado",
    "04": "aceptado con observaciones",
    "05": "anulado",
    "06": "con errores",
    "07": "XML por validar",
    "08": "enviado, por procesar",
    "09": "enviado, procesando",
    "10": "rechazado por SUNAT",
    "11": "CDR descargado",
    "12": "CDR descargado con observaciones",
}

# Estados SFS que se reintentan. Solo el '10' (rechazado): un rechazo deja el
# comprobante sin emitir, así que corresponde volver a intentarlo. El '05' NO va
# acá aunque antes estuviera — es ENVIADO_ANULADO, no un error, y reintentarlo
# significaba reenviar a SUNAT un documento que se había anulado.
_ESTADOS_ERROR = ("10",)
# Estados SFS terminales que el daemon NO puede resolver solo: o el SFS generó el XML
# pero no lo envió (boleta de más de 5 días que exige resumen diario, rechazo de
# validación), o el documento quedó anulado. Reintentar no sirve —el resultado sería
# el mismo—, así que se reportan en cada ciclo para que alguien los atienda a mano.
# El '10' entra en la lista porque resetear_rechazados() corre ANTES del reporte y
# borra los rechazos que todavía tienen reintentos disponibles: si un '10' sigue en
# la tabla al momento de reportar, es porque agotó el tope.
_ESTADOS_BLOQUEADO = ("05", "06", "10")
# Estados en los que el SFS ya cerró el documento con SUNAT: sus archivos de DATA
# no se vuelven a necesitar y hay que borrarlos. Son 5 por comprobante, así que a
# 300 diarios se acumulan 1500 archivos por día en la carpeta que el SFS relee en
# cada pasada.
_ESTADOS_CERRADOS = ("03", "04")

# Cuántas veces se reenvía un comprobante que SUNAT rechazó. El reenvío manda
# exactamente los mismos datos, así que si el rechazo es por un dato mal armado el
# resultado no cambia: sin tope, el daemon reenvía cada ciclo indefinidamente. Al
# agotarse se reporta como bloqueado y espera corrección manual.
MAX_REINTENTOS_RECHAZO = int(os.getenv("MAX_REINTENTOS_RECHAZO", "3"))

# El contador vive en disco: en memoria, un reinicio de PM2 —que reinicia solo— haría
# arrancar la cuenta de cero y el bucle volvería a ser infinito.
_REINTENTOS_PATH = os.path.join(_BASE, "reintentos.json")
_lock_reintentos = threading.Lock()
# Cuántos bloqueados se detallan en el log antes de resumir; son estables entre
# ciclos y volcarlos todos cada 60s ahoga el resto del log.
_MAX_BLOQUEADOS_LOG = 10
# Tipos que el daemon envía hoy: factura, boleta, nota de crédito y nota de débito.
# RC (resumen diario) y RA (comunicación de baja) quedan fuera a propósito: por ahora
# no se emiten desde acá.
_TIPOS_SFS = {"01", "03", "07", "08"}

# Notas: el SFS las parsea con PipeNotaCreditoParser / PipeNotaDebitoParser, que
# esperan una cabecera de 21 columnas —sin fecVencimiento y con
# codMotivo|desMotivo|tipDocAfectado|numDocAfectado después de moneda—. Esos 4
# campos son los que la plantilla del SFS convierte en el <cac:DiscrepancyResponse>
# y el <cac:BillingReference> del XML, que SUNAT exige en toda nota.
_TIPOS_NOTA = {"07", "08"}

# El archivo .PAG genera un cac:PaymentTerms con PaymentMeansID='Contado', y solo la
# factura lo admite así. Los demás validadores lo rechazan:
#   boleta (03): ValidaExprRegBoleta no conoce 'FormaPago' y lee ese nodo como
#                información de detracción -> error 3128 salvo operación 1001-1004.
#   notas (07/08): en una nota el nodo sirve únicamente para crédito y cuotas; el
#                valor debe ser 'Credito' o empezar con 'Cuota' -> error 3246.
# Si algún día se emiten notas al crédito, la forma de pago vuelve pero con ese
# formato, no con 'Contado'.
_TIPOS_SIN_FORMA_PAGO = {"03", "07", "08"}

# Todos los parsers del SFS exigen 36 columnas en el detalle. Ojo con la nota de
# débito: su mensaje de error dice "(30 columnas)", pero el bytecode compara contra
# 36 igual que el resto. Guiarse por ese texto hace que el SFS rechace el archivo
# con un mensaje que apunta justo al número equivocado.
_COLS_DET = 36

# El SFS identifica cada documento por su archivo de cabecera, y la extensión cambia
# según el tipo (ver BandejaDocumentosServiceImpl): .CAB para factura y boleta, .NOT
# para las notas. Con la cabecera en el archivo equivocado el SFS ni siquiera
# reconoce el documento: responde "El archivo no existe: ...NOT".
_EXT_CABECERA = {"07": "NOT", "08": "NOT"}
_EXT_CABECERA_POR_DEFECTO = "cab"

# Todas las extensiones que el daemon puede escribir en DATA. Ningún comprobante
# las lleva todas: la cabecera es .cab o .NOT según el tipo, y el .PAG solo va en
# facturas. Se listan juntas para armar rutas y para barrer al limpiar.
_EXT_DATA = ("cab", "NOT", "det", "tri", "ley", "PAG")
# Las que además puede dejar el SFS (resumen y reversión); se barren al limpiar.
_EXT_DATA_SFS = ("RDI", "TRD", "DET")

# Catálogos SUNAT 09 (nota de crédito) y 10 (nota de débito). Solo se usan para
# completar desMotivo cuando la BD no trae descripción; el código siempre sale de
# Comprobantes.tipo_nota, nunca se infiere.
_MOTIVOS_NOTA = {
    "07": {
        "01": "ANULACION DE LA OPERACION",
        "02": "ANULACION POR ERROR EN EL RUC",
        "03": "CORRECCION POR ERROR EN LA DESCRIPCION",
        "04": "DESCUENTO GLOBAL",
        "05": "DESCUENTO POR ITEM",
        "06": "DEVOLUCION TOTAL",
        "07": "DEVOLUCION POR ITEM",
        "08": "BONIFICACION",
        "09": "DISMINUCION EN EL VALOR",
        "10": "OTROS CONCEPTOS",
        "11": "AJUSTES DE OPERACIONES DE EXPORTACION",
        "12": "AJUSTES AFECTOS AL IVAP",
        "13": "AJUSTES - MONTOS Y/O FECHAS DE PAGO",
    },
    "08": {
        "01": "INTERES POR MORA",
        "02": "AUMENTO EN EL VALOR",
        "03": "PENALIDADES / OTROS CONCEPTOS",
        "11": "AJUSTES DE OPERACIONES DE EXPORTACION",
        "12": "AJUSTES AFECTOS AL IVAP",
    },
}

# Un mismo documento no se reintenta antes de este lapso. Solo evita llamadas
# repetidas dentro del mismo ciclo: debe ser MENOR al intervalo de generación para
# que el ciclo siguiente pueda avanzar un documento que quedó a medio procesar.
# El reenvío queda acotado por el estado, no por el tiempo: _activar_pendientes_sfs_bd()
# solo mira IND_SITU '01'/'02' (aún sin enviar); ver _ESTADOS_BLOQUEADO para el resto.
_COOLDOWN_REENVIO_SEG = 45
_ultimo_intento: dict = {}

# Pausas al conversar con el SFS. No son arbitrarias: el facturador procesa los
# archivos de DATA en background, así que hay que darle tiempo entre el pedido de
# generación y el de envío o responde "No existen datos que procesar".
_ESPERA_XML_SEG        = 2   # tras pedir la generación del XML
_ESPERA_REINTENTO_SEG  = 3   # antes de reintentar un envío que falló
_ESPERA_ENTRE_DOCS_SEG = 1   # para no saturar al SFS documento tras documento

# Estados de la columna Comprobantes.enviado.
# Es un BIT: solo admite 0/1 (SQL Server convierte cualquier no-cero a 1), así que
# NO sirve para un estado intermedio. El "entregado al SFS, esperando CDR" se
# deduce de la BD del SFS — ver _docs_en_vuelo().
ENVIADO_PENDIENTE = 0   # por generar / reintentar
ENVIADO_ACEPTADO  = 1   # SUNAT devolvió un CDR de aceptación

# Estados de CDR que dan por buena la emisión (SUNAT acepta con y sin observaciones)
_CDR_ACEPTADOS = {"ACEPTADO", "OBSERVADO"}

# Comprobantes.errors es nvarchar(4000); el motivo se recorta antes de guardarlo.
_MAX_ERRORS_SQL = 4000

# Serializa el barrido de RPTA: watchdog lanza una llamada por cada CDR que aparece
# y todas recorren el directorio completo (ver procesar_respuestas).
_lock_cdr = threading.Lock()

# Código ODBC de DATETIMEOFFSET: pyodbc no lo mapea solo (ver _handle_datetimeoffset)
_SQL_DATETIMEOFFSET = -155

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


def _texto(valor, defecto: str = "") -> str:
    """
    Valor de BD como texto limpio, con respaldo si viene vacío o nulo. Evita el
    str(None) == "None" que se colaba a los archivos cuando la columna era NULL.
    """
    return str(valor if valor is not None else "").strip() or defecto


def _codigo(valor, defecto: str = "") -> str:
    """Código SUNAT de dos dígitos: '1' -> '01'. Devuelve el respaldo si no hay dato."""
    texto = _texto(valor)
    return texto.zfill(2) if texto else defecto


def _campo_pipe(valor, defecto: str = "") -> str:
    """Texto apto para un archivo delimitado por pipes."""
    return re.sub(r"[|\r\n\t]+", " ", _texto(valor)).strip() or defecto


def _marcas(cantidad: int) -> str:
    """Placeholders '?,?,?' para un IN de SQL."""
    return ",".join("?" * cantidad)


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


def _borrar_si_existe(ruta: str):
    """
    Borra un archivo que puede no estar. Se usa al regenerar un comprobante: el SFS
    levanta todo lo que encuentre en DATA, así que un archivo sobrante de una
    emisión anterior se colaría en la nueva.
    """
    try:
        os.remove(ruta)
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("No se pudo borrar %s", ruta)


def _mover(ruta: str, carpeta: str):
    os.makedirs(carpeta, exist_ok=True)
    try:
        os.replace(ruta, os.path.join(carpeta, os.path.basename(ruta)))
    except Exception:
        logger.exception("No se pudo mover %s a %s", ruta, carpeta)

# ---------------------------------------------------------------------------
# Base de datos — SQL Server
# ---------------------------------------------------------------------------

def _handle_datetimeoffset(valor: bytes) -> datetime:
    """
    pyodbc no sabe convertir DATETIMEOFFSET y lo entrega crudo; sin este converter
    cualquier SELECT que toque una columna de ese tipo revienta con HY106.
    """
    anio, mes, dia, hora, minu, seg, nanos, off_h, off_m = struct.unpack("<6hI2h", valor)
    return datetime(
        anio, mes, dia, hora, minu, seg, nanos // 1000,
        timezone(timedelta(hours=off_h, minutes=off_m)),
    )


def conectar_bd():
    conn_str = (
        f"DRIVER={DB_CONFIG['driver']};SERVER={DB_CONFIG['server']};"
        f"DATABASE={DB_CONFIG['database']};Trusted_Connection={DB_CONFIG['trusted_connection']};"
    )
    conn = pyodbc.connect(conn_str, timeout=DB_CONFIG["timeout"])
    conn.add_output_converter(_SQL_DATETIMEOFFSET, _handle_datetimeoffset)
    return conn


def _consultar(conn, sql: str, params: tuple = ()) -> list:
    """Filas de un SELECT como diccionarios columna -> valor."""
    cur = conn.cursor()
    cur.execute(sql, params)
    columnas = [d[0] for d in cur.description]
    return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def _actualizar(conn, sql: str, params: tuple) -> int:
    """UPDATE con commit. Devuelve las filas afectadas, o -1 si la sentencia falló."""
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        return cur.rowcount
    except Exception:
        logger.exception("Error ejecutando UPDATE: %s", sql)
        return -1


@contextmanager
def _sfs_bd(escritura: bool = False):
    """
    Conexión a la BD SQLite del SFS. Hace falta closing() además del `with` porque el
    context manager de sqlite3 hace commit/rollback pero NO cierra la conexión; con
    escritura=True la transacción se confirma al salir.
    """
    conexion = sqlite3.connect(SFS_BD_PATH)
    with closing(conexion):
        if escritura:
            with conexion:
                yield conexion
        else:
            yield conexion


def obtener_emisor(conn):
    filas = _consultar(conn, "SELECT TOP 1 ruc, razon_social FROM Emisores")
    return filas[0] if filas else None


def obtener_receptor(conn, receptor_id):
    if receptor_id is None:
        return {}
    filas = _consultar(conn, "SELECT TOP 1 * FROM Receptores WHERE id = ?", (int(receptor_id),))
    return filas[0] if filas else {}


def obtener_items(conn, comprobante_id):
    return _consultar(conn, "SELECT * FROM Items WHERE ComprobanteId = ?", (int(comprobante_id),))


def obtener_comprobantes_pendientes(conn):
    return _consultar(
        conn,
        "SELECT * FROM Comprobantes WHERE enviado IS NULL OR enviado = 0 ORDER BY fecha_emision ASC",
    )

# ---------------------------------------------------------------------------
# Generador de archivos SFS
# ---------------------------------------------------------------------------

def _nombre_base(ruc: str, tipo: str, num: str) -> str:
    serie, corr = num.split("-", 1) if "-" in num else ("0000", num or "00000000")
    nombre = f"{ruc}-{tipo}-{serie}-{corr}"
    return re.sub(r'[<>:"/\\|?*\n\r\t]+', "_", nombre.strip())[:250]


def _referencia_nota(comp: dict, tipo_comp: str, num_comp: str):
    """
    (codMotivo, desMotivo, tipDocAfectado, numDocAfectado) para una nota, o None si
    falta algo. El código de motivo NO se deduce ni se rellena por defecto: es un dato
    tributario y una nota con el motivo equivocado es una declaración incorrecta ante
    SUNAT. Sin él la nota no se emite y queda reportada para que la completen.
    """
    cod_motivo   = _codigo(comp.get("tipo_nota"))
    tip_afectado = _codigo(comp.get("tipo_documento_afectado"))
    num_afectado = _campo_pipe(comp.get("numeracion_documento_afectado"))

    faltantes = [
        nombre for nombre, valor in (
            ("tipo_nota (código de motivo)",        cod_motivo),
            ("tipo_documento_afectado",             tip_afectado),
            ("numeracion_documento_afectado",       num_afectado),
        ) if not valor
    ]
    if faltantes:
        logger.warning(
            "Nota %s-%s sin datos de referencia (%s); no se emite hasta completarlos.",
            tipo_comp, num_comp, ", ".join(faltantes),
        )
        return None

    des_motivo = _campo_pipe(
        comp.get("motivo_documento_afectado"),
        _MOTIVOS_NOTA.get(tipo_comp, {}).get(cod_motivo, "OTROS CONCEPTOS"),
    )
    return cod_motivo, des_motivo, tip_afectado, num_afectado


def _linea_detalle(item: dict) -> str:
    """Una línea del archivo .det: las 36 columnas en el orden que lee el SFS."""
    cant   = formatear_decimal(item.get("dec_cantidad") or item.get("cantidad_venta") or item.get("cantidad", 1))
    v_unit = formatear_decimal(item.get("valor"))
    v_vta  = formatear_decimal(item.get("valor_venta"))
    igv_it = formatear_decimal(item.get("igv_venta"))
    p_unit = formatear_decimal(item.get("precio"))

    campos = [
        _campo_pipe(item.get("medida"), "NIU"),
        f"{cant:.2f}",
        _campo_pipe(item.get("codigo_producto"), "-"),
        "-",
        _campo_pipe(item.get("descripcion"), "ITEM"),
        f"{v_unit:.6f}",
        f"{igv_it:.2f}", "1000", f"{igv_it:.2f}", f"{v_vta:.2f}", "IGV", "VAT", "10", "18.00",
    ] + ["-"] * 19 + [
        f"{p_unit:.2f}", f"{v_vta:.2f}", "0.00",
    ]
    return "|".join(campos[:_COLS_DET]) + "|\n"


def procesar_comprobante(conn, comp: dict, ruc_emisor: str) -> bool:
    num_comp = _texto(comp.get("numeracion_comprobante"))
    # zfill(2) para que el nombre de archivo coincida con el tip_docu que se manda al SFS
    tipo_comp = _codigo(comp.get("tipo_comprobante"), "01")

    # Las notas se validan antes de escribir nada: si les falta la referencia, el SFS
    # las rechazaría igual y quedarían archivos huérfanos en DATA.
    referencia = None
    if tipo_comp in _TIPOS_NOTA:
        referencia = _referencia_nota(comp, tipo_comp, num_comp)
        if referencia is None:
            return False

    base  = _nombre_base(ruc_emisor, tipo_comp, num_comp)
    os.makedirs(SFS_DATA_DIR, exist_ok=True)
    ext_cab = _EXT_CABECERA.get(tipo_comp, _EXT_CABECERA_POR_DEFECTO)
    rutas = {e: os.path.join(SFS_DATA_DIR, f"{base}.{e}") for e in _EXT_DATA}
    rutas["cabecera"] = rutas[ext_cab]

    receptor     = obtener_receptor(conn, comp.get("ReceptorId"))
    tipo_doc_rec = _campo_pipe(receptor.get("tipo_documento"),   "0")
    num_doc_rec  = _campo_pipe(receptor.get("numero_documento"), "00000000")
    razon_social = _campo_pipe(receptor.get("razon_social"),     "CLIENTE VARIOS")
    moneda       = _campo_pipe(comp.get("tipo_moneda"),          "PEN")
    monto_letras = _campo_pipe(comp.get("monto_letras"),         "SIN DESCRIPCION")

    fecha_dt  = formatear_fecha_hora(comp.get("fecha_emision"))
    fecha_str = fecha_dt.strftime("%Y-%m-%d")
    hora_str  = fecha_dt.strftime("%H:%M:%S")

    tot_grav  = formatear_decimal(comp.get("gravadas")  or comp.get("total_gravadas"))
    tot_igv   = formatear_decimal(comp.get("igv")       or comp.get("total_igv"))
    tot_venta = formatear_decimal(comp.get("total")     or comp.get("total_venta"))

    lineas_det = [_linea_detalle(item) for item in obtener_items(conn, comp["id"])]
    if not lineas_det:
        logger.warning("Comprobante %s sin ítems.", num_comp)

    # Cola de la cabecera: totales y versiones, iguales en los dos layouts.
    totales = (
        f"{tot_igv:.2f}|{tot_grav:.2f}|{tot_venta:.2f}|"
        f"0.00|0.00|0.00|{tot_venta:.2f}|2.1|2.0|\n"
    )
    if referencia is not None:
        # Cabecera de nota (21 columnas, archivo .NOT): sin fecVencimiento y con la
        # referencia al documento afectado, que es lo que SUNAT exige en toda nota.
        cod_motivo, des_motivo, tip_afectado, num_afectado = referencia
        escribir_archivo(rutas["cabecera"],
            f"0101|{fecha_str}|{hora_str}|0000|{tipo_doc_rec}|{num_doc_rec}|"
            f"{razon_social}|{moneda}|{cod_motivo}|{des_motivo}|{tip_afectado}|{num_afectado}|"
            + totales
        )
        # El SFS reconoce el tipo por la extensión de la cabecera: un .cab sobrante
        # haría que tome la nota por una factura.
        _borrar_si_existe(rutas["cab"])
    else:
        # Cabecera de factura/boleta (18 columnas, archivo .cab).
        escribir_archivo(rutas["cabecera"],
            f"0101|{fecha_str}|{hora_str}|-|0000|{tipo_doc_rec}|{num_doc_rec}|"
            f"{razon_social}|{moneda}|" + totales
        )

    if tipo_comp in _TIPOS_SIN_FORMA_PAGO:
        _borrar_si_existe(rutas["PAG"])
    else:
        escribir_archivo(rutas["PAG"], f"Contado|{tot_venta:.2f}|{moneda}|\n")

    escribir_archivo(rutas["tri"], f"1000|IGV|VAT|{tot_grav:.2f}|{tot_igv:.2f}|\n")
    escribir_archivo(rutas["ley"], f"1000|{monto_letras}|\n")

    if lineas_det:
        escribir_archivo(rutas["det"], "".join(lineas_det))
    else:
        _borrar_si_existe(rutas["det"])

    # OJO: no se toca Comprobantes.enviado acá. Generar los archivos no es haber
    # enviado nada; el estado lo mueve ciclo_generacion() recién cuando el SFS
    # confirma la recepción, y lo cierra el CDR de SUNAT.
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


def _resumen_sfs(r) -> str:
    """
    Respuesta del SFS en una línea. Sin esto, un solo fallo vuelca al log toda la
    bandeja (listaBandejaFacturador), que son decenas de miles de caracteres.
    """
    if not isinstance(r, dict):
        return repr(r)
    partes = [f"{k}={r[k]!r}" for k in ("validacion", "mensaje") if r.get(k)]
    for clave, valor in r.items():
        if isinstance(valor, list):
            partes.append(f"{clave}=[{len(valor)} items]")
    return ", ".join(partes) or repr(r)


def _xml_generado(ruc: str, tip: str, num: str) -> bool:
    """True si el SFS ya generó el XML del documento (FEC_GENE con valor)."""
    if not os.path.exists(SFS_BD_PATH):
        return True  # sin BD del SFS no se puede comprobar; no bloquear el envío
    try:
        with _sfs_bd() as sfs:
            fila = sfs.execute(
                "SELECT FEC_GENE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                (ruc, tip, num),
            ).fetchone()
        return bool(fila and fila[0])
    except sqlite3.Error:
        return True


def activar_procesamiento_sfs(documentos: list) -> list:
    """Envía los documentos al SFS local. Devuelve solo los que el SFS aceptó."""
    if not documentos:
        return []
    try:
        urllib.request.urlopen(f"{SFS_BASE_URL}/", timeout=3)
    except Exception:
        logger.warning("SFS no responde — envío automático desactivado.")
        return []

    enviados = []
    ahora    = time.monotonic()
    # El cooldown solo evita repetir un documento dentro del mismo ciclo, así que las
    # marcas vencidas no sirven de nada: sin purgarlas el diccionario crece un registro
    # por comprobante y nunca libera, en un proceso pensado para correr meses.
    for clave in [k for k, t in _ultimo_intento.items() if ahora - t >= _COOLDOWN_REENVIO_SEG]:
        del _ultimo_intento[clave]

    for doc in documentos:
        tip   = _texto(doc.get("tip_docu"))
        num   = _texto(doc.get("num_docu"))
        label = f"{tip}-{num}"
        if tip not in _TIPOS_SFS:
            logger.info("[SFS] Tipo %s fuera de alcance, omitido: %s", tip, label)
            continue

        previo = _ultimo_intento.get((tip, num))
        if previo is not None and ahora - previo < _COOLDOWN_REENVIO_SEG:
            continue
        _ultimo_intento[(tip, num)] = ahora

        payload = {k: doc[k] for k in ("num_ruc", "tip_docu", "num_docu")}

        r1 = _sfs_post("api/GenerarComprobante.htm", payload)
        if not (r1 and r1.get("validacion") == "EXITO"):
            logger.warning("[SFS] Error al generar XML para %s: %s", label, _resumen_sfs(r1))
            continue

        time.sleep(_ESPERA_XML_SEG)
        # El SFS trabaja en dos pasadas: la 1ra solo registra el archivo de DATA en
        # su bandeja (IND_SITU='01'); recién la 2da genera el XML ('02'). Sin este
        # segundo llamado, enviarXML responde "No existen datos que procesar".
        if not _xml_generado(_texto(doc.get("num_ruc")), tip, num):
            _sfs_post("api/GenerarComprobante.htm", payload)
            time.sleep(_ESPERA_XML_SEG)

        r2 = _sfs_post("api/enviarXML.htm", payload)
        if not (r2 and r2.get("validacion") == "EXITO"):
            time.sleep(_ESPERA_REINTENTO_SEG)
            r2 = _sfs_post("api/enviarXML.htm", payload)

        # OJO: "EXITO" solo dice que el SFS aceptó el pedido. NO garantiza que
        # SUNAT lo haya recibido — el SFS puede dejarlo en IND_SITU='06' (p.ej.
        # boletas de más de 5 días, que exigen resumen diario). Lo confirma el CDR.
        if r2 and r2.get("validacion") == "EXITO":
            logger.info("[SFS] Entregado al SFS: %s", label)
            enviados.append(doc)
        elif "no existen datos" in str((r2 or {}).get("mensaje", "")).lower():
            # Primera pasada: el SFS aún no generó el XML. Es el flujo normal,
            # no un error — el próximo ciclo lo retoma desde IND_SITU='01'.
            logger.info("[SFS] %s aún sin XML; se completa en el próximo ciclo.", label)
        else:
            logger.warning("[SFS] Error al entregar %s: %s", label, _resumen_sfs(r2))
        time.sleep(_ESPERA_ENTRE_DOCS_SEG)
    return enviados

# ---------------------------------------------------------------------------
# SFS BD SQLite — gestión de estados
# ---------------------------------------------------------------------------

def _registrar_en_sfs_bd(ruc_emisor: str, docs: list):
    if not os.path.exists(SFS_BD_PATH):
        return
    time.sleep(_ESPERA_XML_SEG)
    with _sfs_bd(escritura=True) as sfs:
        for doc in docs:
            tip = _texto(doc.get("tip_docu"))
            if tip not in _TIPOS_SFS:
                continue
            num  = _texto(doc.get("num_docu"))
            arch = f"{ruc_emisor}-{tip}-{num}"
            existe = sfs.execute(
                "SELECT 1 FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                (ruc_emisor, tip, num),
            ).fetchone()
            if not existe:
                # Estado pendiente, no '03'/aceptado: SUNAT todavía no respondió.
                sfs.execute(
                    "INSERT INTO DOCUMENTO (NUM_RUC, TIP_DOCU, NUM_DOCU, NOM_ARCH, IND_SITU, DES_OBSE) "
                    "VALUES (?,?,?,?,?,?)",
                    (ruc_emisor, tip, num, arch, "01", "Enviado al SFS, esperando CDR"),
                )


def _tiene_cdr(ruc: str, tip: str, num: str) -> bool:
    nombre = f"R{ruc}-{tip}-{num}.zip"
    return any(os.path.exists(os.path.join(d, nombre)) for d in (SFS_RPTA_DIR, DIR_PROCESADOS))


def _eliminar_data_files(nom_arch: str):
    for ext in _EXT_DATA + _EXT_DATA_SFS:
        _borrar_si_existe(os.path.join(SFS_DATA_DIR, f"{nom_arch}.{ext}"))


def _limpiar_data_cerrados(ruc_emisor: str, en_vuelo: dict) -> int:
    """
    Borra de DATA los archivos de los comprobantes que el SFS ya cerró con SUNAT.

    Se recorre la carpeta y no la tabla DOCUMENTO a propósito: DATA solo tiene lo
    pendiente más lo recién cerrado, mientras que DOCUMENTO es el histórico y crece
    sin límite. Así el trabajo por ciclo es proporcional a lo que queda por limpiar.
    """
    if not os.path.isdir(SFS_DATA_DIR):
        return 0
    prefijo = f"{ruc_emisor}-"
    bases = {
        os.path.splitext(nombre)[0]
        for nombre in os.listdir(SFS_DATA_DIR)
        if nombre.startswith(prefijo)
    }
    borrados = 0
    for base in bases:
        # base = <ruc>-<tipo>-<serie>-<correlativo>
        partes = base.split("-")
        if len(partes) < 4:
            continue
        tip, num = partes[1], "-".join(partes[2:])
        situ, _ = en_vuelo.get((tip, num), ("", ""))
        if situ in _ESTADOS_CERRADOS:
            _eliminar_data_files(base)
            borrados += 1
    return borrados


def _activar_pendientes_sfs_bd(ruc_emisor: str, ya_procesados: list):
    if not os.path.exists(SFS_BD_PATH):
        return
    ya_keys = {(d["tip_docu"], d["num_docu"]) for d in ya_procesados}
    with _sfs_bd(escritura=True) as sfs:
        tipos = sorted(_TIPOS_SFS)
        rows = sfs.execute(
            "SELECT TIP_DOCU, NUM_DOCU, NOM_ARCH FROM DOCUMENTO "
            f"WHERE NUM_RUC=? AND TIP_DOCU IN ({_marcas(len(tipos))}) "
            "AND IND_SITU IN ('01','02')",
            (ruc_emisor, *tipos),
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


def _docs_en_vuelo(ruc_emisor: str) -> dict:
    """
    {(tip_docu, num_docu): (IND_SITU, DES_OBSE)} de lo que el SFS ya tiene
    registrado y por lo tanto está entregado, en proceso o bloqueado. Se usa para
    no reenviar a SUNAT un comprobante que sigue en enviado=0 solo porque su CDR
    todavía no llegó — y para separar esos de los que están trabados (_ESTADOS_BLOQUEADO).
    """
    if not os.path.exists(SFS_BD_PATH):
        return {}
    try:
        with _sfs_bd() as sfs:
            return {
                (_texto(t), _texto(n)): (_texto(situ), _texto(obse))
                for t, n, situ, obse in sfs.execute(
                    "SELECT TIP_DOCU, NUM_DOCU, IND_SITU, DES_OBSE FROM DOCUMENTO WHERE NUM_RUC=?",
                    (ruc_emisor,),
                )
            }
    except sqlite3.Error:
        logger.exception("No se pudo leer la BD del SFS; se omite el filtro de duplicados.")
        return {}


def _leer_reintentos() -> dict:
    try:
        with open(_REINTENTOS_PATH, encoding="utf-8") as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # Un archivo corrupto no puede frenar la emisión: se empieza de cero y se
        # avisa. El costo es volver a contar desde 1 para los rechazados vigentes.
        logger.exception("No se pudo leer %s; se reinicia el conteo de reintentos.", _REINTENTOS_PATH)
        return {}


def _guardar_reintentos(datos: dict):
    try:
        escribir_archivo(_REINTENTOS_PATH, json.dumps(datos, ensure_ascii=False, indent=2))
    except OSError:
        logger.exception("No se pudo guardar %s; el conteo de reintentos no persiste.", _REINTENTOS_PATH)


def _reintentos_de(numeracion: str) -> int:
    """Cuántos reenvíos lleva el comprobante, sin tocar el contador."""
    with _lock_reintentos:
        registro = _leer_reintentos().get(numeracion) or {}
    try:
        return int(registro.get("intentos", 0))
    except (TypeError, ValueError):
        return 0


def _contar_reintento(numeracion: str, tipo: str, motivo: str = "") -> int:
    """
    Suma un reenvío al comprobante y devuelve cuántos lleva. La clave es la
    numeración, igual que en _actualizar_sql_cdr(), para que el contador se limpie
    solo cuando llegue el CDR de aceptación.
    """
    with _lock_reintentos:
        datos = _leer_reintentos()
        registro = datos.get(numeracion) or {}
        intentos = int(registro.get("intentos", 0)) + 1
        datos[numeracion] = {
            "tipo": tipo,
            "intentos": intentos,
            "ultimo": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "motivo": motivo or registro.get("motivo", ""),
        }
        _guardar_reintentos(datos)
        return intentos


def _limpiar_reintento(numeracion: str):
    """El comprobante salió aceptado: su historial de rechazos deja de importar."""
    with _lock_reintentos:
        datos = _leer_reintentos()
        if datos.pop(numeracion, None) is not None:
            _guardar_reintentos(datos)


def resetear_rechazados(conn, ruc_emisor: str):
    if not os.path.exists(SFS_BD_PATH):
        return
    # Placeholders dinámicos: _ESTADOS_ERROR puede tener uno o varios estados.
    marcas = _marcas(len(_ESTADOS_ERROR))
    with _sfs_bd(escritura=True) as sfs:
        rows = sfs.execute(
            f"SELECT NUM_DOCU, TIP_DOCU, DES_OBSE FROM DOCUMENTO "
            f"WHERE NUM_RUC=? AND IND_SITU IN ({marcas})",
            (ruc_emisor, *_ESTADOS_ERROR),
        ).fetchall()
        if not rows:
            return
        cur = conn.cursor()
        reintentados, agotados = [], []
        for num_docu, tip_docu, des_obse in rows:
            # Se consulta antes de sumar: al agotarse, el documento se queda en '10'
            # y esta rama corre en cada ciclo. Sumando siempre, el contador crecería
            # sin sentido y reescribiría el archivo cada 60 segundos para siempre.
            if _reintentos_de(num_docu) >= MAX_REINTENTOS_RECHAZO:
                # Se deja la fila en DOCUMENTO: así el comprobante sigue contando como
                # "en vuelo" —no se regenera— y el reporte de bloqueados lo levanta.
                agotados.append((tip_docu, num_docu))
                continue
            intentos = _contar_reintento(num_docu, tip_docu, _texto(des_obse))
            cur.execute(
                "UPDATE Comprobantes SET enviado=? WHERE numeracion_comprobante=? AND tipo_comprobante=?",
                (ENVIADO_PENDIENTE, num_docu, tip_docu),
            )
            sfs.execute(
                f"DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                f"AND IND_SITU IN ({marcas})",
                (ruc_emisor, tip_docu, num_docu, *_ESTADOS_ERROR),
            )
            reintentados.append((tip_docu, num_docu, intentos))
    conn.commit()

    if reintentados:
        logger.info(
            "%d comprobante(s) rechazados vuelven a la cola: %s",
            len(reintentados),
            ", ".join(f"{t}-{n} (intento {i}/{MAX_REINTENTOS_RECHAZO})" for t, n, i in reintentados),
        )
    if agotados:
        # Reenviar de nuevo daría el mismo rechazo: los datos son idénticos.
        logger.error(
            "%d comprobante(s) agotaron los %d reenvíos permitidos y NO se reenviarán "
            "hasta que se corrija el dato observado: %s",
            len(agotados), MAX_REINTENTOS_RECHAZO,
            ", ".join(f"{t}-{n}" for t, n in agotados),
        )

# ---------------------------------------------------------------------------
# Parser CDR — respuestas SUNAT
# ---------------------------------------------------------------------------

def _iter_elementos(elem, ancs=()):
    yield elem, ancs
    for hijo in elem:
        yield from _iter_elementos(hijo, ancs + (elem.tag.split("}")[-1],))


def _extraer_numeracion(texto) -> str | None:
    # La serie SUNAT son 4 caracteres alfanuméricos que arrancan con letra: F001,
    # B001, y también BC01/FC01/BC03 en notas de crédito. El patrón anterior exigía
    # 3 dígitos al final (\d{3}) y dejaba fuera esas series, con lo que el CDR de una
    # nota de crédito quedaba sin numeración y su comprobante nunca pasaba a enviado=1.
    m = re.search(r"[A-Z][A-Z0-9]{3}-\d+", _texto(texto))
    return m.group(0) if m else None


def parsear_xml_cdr(fuente) -> dict:
    res = {"numeracion": None, "codigo": None, "descripcion": None, "status": "PENDIENTE"}
    try:
        root = ET.fromstring(fuente) if isinstance(fuente, bytes) else ET.parse(fuente).getroot()
        # Las descripciones que cuelgan de un <Response> son las buenas; cualquier otra
        # queda de respaldo por si el CDR no trae ninguna en el lugar esperado.
        descripciones, respaldo = [], []
        for elem, ancs in _iter_elementos(root):
            if not isinstance(elem.tag, str):
                continue
            tag  = elem.tag.split("}")[-1].lower()
            text = _texto(elem.text)
            if not text:
                continue
            if tag == "responsecode" and not res["codigo"]:
                res["codigo"] = text
            elif tag in {"description", "responsedescription"}:
                if any("response" in a.lower() for a in ancs):
                    descripciones.append(text)
                elif tag == "description" and not respaldo:
                    respaldo.append(text)
            elif tag in {"referenceid", "id"} and not res["numeracion"]:
                res["numeracion"] = _extraer_numeracion(text)

        res["descripcion"] = " | ".join(dict.fromkeys(descripciones or respaldo)) or None

        if not res["numeracion"]:
            res["numeracion"] = _extraer_numeracion(res["descripcion"])
        if not res["numeracion"] and isinstance(fuente, str):
            res["numeracion"] = _extraer_numeracion(os.path.basename(fuente))

        codigo = _texto(res["codigo"])
        desc   = _texto(res["descripcion"]).lower()
        # El ResponseCode manda: SUNAT solo devuelve 0 cuando acepta. Cualquier
        # otro código es rechazo, aunque la descripción no diga "rechazado".
        if codigo:
            res["status"] = "ACEPTADO" if codigo.strip("0") == "" else "RECHAZADO"
        elif "acept" in desc:
            res["status"] = "ACEPTADO"
        elif "rechaz" in desc or "error" in desc or "no autorizado" in desc:
            res["status"] = "RECHAZADO"

        # Aceptada con observaciones sigue siendo aceptada (ver _CDR_ACEPTADOS)
        if res["status"] == "ACEPTADO" and "observ" in desc:
            res["status"] = "OBSERVADO"

    except Exception:
        logger.exception("Error parseando CDR %s", fuente if isinstance(fuente, str) else "<bytes>")
        res["status"] = "ERROR"
    return res


def _actualizar_sql_cdr(conn, numeracion: str, parsed: dict) -> bool:
    if not numeracion:
        logger.error(
            "CDR aceptado sin numeración reconocible (código %s): %s — "
            "el comprobante queda en enviado=0.",
            parsed.get("codigo"), parsed.get("descripcion"),
        )
        return False
    if parsed["status"] not in _CDR_ACEPTADOS:
        return False
    # errors se limpia junto con la aceptación: si el comprobante había sido rechazado
    # antes, el motivo viejo ya no aplica.
    filas = _actualizar(
        conn,
        "UPDATE Comprobantes SET enviado = ?, errors = NULL WHERE numeracion_comprobante = ?",
        (ENVIADO_ACEPTADO, numeracion),
    )
    if filas > 0:
        _limpiar_reintento(numeracion)
        return True
    if filas == 0:
        # SUNAT aceptó algo que no está en Comprobantes: numeración con otro formato,
        # comprobante borrado, o CDR de otro emisor. Silenciarlo dejaba el documento
        # en enviado=0 y el ZIP archivado como procesado, o sea perdido.
        logger.error(
            "CDR aceptado de %s pero ningún comprobante coincide en SQL Server; "
            "queda en enviado=0.", numeracion,
        )
    return False


def _registrar_error_cdr(conn, numeracion: str, parsed: dict) -> bool:
    """
    Deja el motivo del rechazo en Comprobantes.errors. Sin esto el código de SUNAT
    solo vive en facturador.log, y quien mira la BD no tiene forma de saber por qué
    un comprobante sigue en enviado=0.
    """
    if not numeracion:
        return False
    detalle = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] CDR {parsed['status']}"
    if parsed.get("codigo"):
        detalle += f" — código {parsed['codigo']}"
    if parsed.get("descripcion"):
        detalle += f": {parsed['descripcion']}"
    filas = _actualizar(
        conn,
        "UPDATE Comprobantes SET errors = ? WHERE numeracion_comprobante = ?",
        (detalle[:_MAX_ERRORS_SQL], numeracion),
    )
    return filas > 0


def _archivo_estable(ruta: str, intentos: int = 5, espera: float = 0.5) -> bool:
    """
    True cuando el tamaño del archivo dejó de cambiar. SUNAT/SFS deja el ZIP en RPTA
    mientras todavía lo escribe y watchdog avisa apenas se crea: abrirlo de inmediato
    daba BadZipFile —y lo mandaba a errores/— sobre un archivo que estaba sano.
    """
    ultimo = -1
    for _ in range(intentos):
        try:
            actual = os.path.getsize(ruta)
        except OSError:
            return False
        if actual > 0 and actual == ultimo:
            return True
        ultimo = actual
        time.sleep(espera)
    return False


def procesar_respuestas():
    """
    Barre RPTA. El lock serializa las llamadas: watchdog dispara una por cada CDR que
    llega y todas recorren el mismo directorio, así que sin esto dos hilos parsean y
    mueven el mismo archivo a la vez. Bloqueante a propósito —el que espera vuelve a
    listar el directorio al entrar— para que ningún CDR quede sin barrer.
    """
    with _lock_cdr:
        _barrer_rpta()


def _barrer_rpta():
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
                if not _archivo_estable(ruta):
                    # Lo retoma el barrido periódico de hilo_cdr; no cuenta como error.
                    logger.info("CDR %s aún se está escribiendo; se retoma luego.", nombre)
                    continue
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

                if parsed["status"] in _CDR_ACEPTADOS:
                    if _actualizar_sql_cdr(conn, num, parsed):
                        ok += 1
                        _mover(ruta, DIR_PROCESADOS)
                    else:
                        # Aceptado por SUNAT pero no se pudo cerrar en SQL Server
                        # (sin numeración o sin fila que coincida). Archivarlo como
                        # procesado lo hacía desaparecer con el comprobante en
                        # enviado=0: va a errores/ para que quede a la vista.
                        _mover(ruta, DIR_ERRORES)
                        err += 1
                else:
                    # Un rechazo no se archiva como procesado: queda en errores/
                    # para revisión manual y el comprobante NO pasa a aceptado.
                    logger.error(
                        "CDR %s de %s (%s) — código %s: %s",
                        parsed["status"], num, nombre, parsed["codigo"], parsed["descripcion"],
                    )
                    if not _registrar_error_cdr(conn, num, parsed):
                        logger.warning(
                            "El motivo del rechazo de %s no se pudo guardar en la BD; "
                            "queda solo en este log.", num,
                        )
                    _mover(ruta, DIR_ERRORES)
                    err += 1

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

def _reportar_clasificacion(fuera_alcance: int, omitidos: int, bloqueados: list):
    """Resume en el log qué pasó con los pendientes que no se generaron este ciclo."""
    if fuera_alcance:
        logger.info(
            "%d comprobante(s) de tipo fuera de alcance (solo se emiten %s).",
            fuera_alcance, ", ".join(sorted(_TIPOS_SFS)),
        )
    if omitidos:
        logger.info("%d comprobante(s) ya entregados al SFS, esperando CDR.", omitidos)
    if not bloqueados:
        return
    # No son "esperando CDR": el SFS nunca los mandó y no lo va a hacer solo.
    logger.warning(
        "%d comprobante(s) BLOQUEADOS en el SFS — requieren intervención manual:",
        len(bloqueados),
    )
    for tip, num, situ, obse in bloqueados[:_MAX_BLOQUEADOS_LOG]:
        logger.warning("    %s-%s [%s]: %s", tip, num, _NOMBRE_SITU.get(situ, situ), obse or "sin detalle")
    if len(bloqueados) > _MAX_BLOQUEADOS_LOG:
        logger.warning("    ... y %d más.", len(bloqueados) - _MAX_BLOQUEADOS_LOG)


def ciclo_generacion():
    logger.info("Consultando BD...")
    conn = None
    try:
        conn = conectar_bd()
        emisor = obtener_emisor(conn)
        if not emisor:
            logger.error("No se encontró información del Emisor en BD.")
            return

        ruc_emisor = EMISOR_RUC_OVERRIDE or _texto(emisor.get("ruc"), "00000000000")

        resetear_rechazados(conn, ruc_emisor)

        comprobantes = obtener_comprobantes_pendientes(conn)
        logger.info("%d comprobante(s) pendiente(s).", len(comprobantes))

        en_vuelo = _docs_en_vuelo(ruc_emisor)
        docs_generados = []
        bloqueados = []
        omitidos = fuera_alcance = 0
        for comp in comprobantes:
            try:
                tip = _codigo(comp.get("tipo_comprobante"), "01")
                num = _texto(comp.get("numeracion_comprobante"))
                # Fuera de alcance: se descarta acá para no regenerar sus archivos
                # en cada ciclo, ya que nunca van a entrar al SFS.
                if tip not in _TIPOS_SFS:
                    fuera_alcance += 1
                    continue
                # Sigue en enviado=0 pero el SFS ya lo tiene: no reenviar. Puede estar
                # esperando CDR o trabado en un estado que el daemon no resuelve solo.
                if (tip, num) in en_vuelo:
                    situ, obse = en_vuelo[(tip, num)]
                    if situ in _ESTADOS_BLOQUEADO:
                        bloqueados.append((tip, num, situ, obse))
                    else:
                        omitidos += 1
                    continue
                if procesar_comprobante(conn, comp, ruc_emisor):
                    docs_generados.append({
                        "num_ruc":  ruc_emisor,
                        "tip_docu": tip,
                        "num_docu": num,
                    })
            except Exception:
                logger.exception("Error procesando %r", comp.get("numeracion_comprobante"))

        _reportar_clasificacion(fuera_alcance, omitidos, bloqueados)

        if docs_generados:
            logger.info("%d comprobante(s) generados, entregando al SFS...", len(docs_generados))
            # Solo se registra lo que el SFS confirmó; lo demás sigue en enviado=0
            # y se reintenta en el próximo ciclo.
            enviados = activar_procesamiento_sfs(docs_generados)
            _registrar_en_sfs_bd(ruc_emisor, enviados)

            no_enviados = len(docs_generados) - len(enviados)
            if no_enviados:
                logger.warning(
                    "%d comprobante(s) no llegaron al SFS; se reintentan en el próximo ciclo.",
                    no_enviados,
                )

        _activar_pendientes_sfs_bd(ruc_emisor, docs_generados)

        # Se relee el estado en vez de reusar el de arriba: lo entregado en este mismo
        # ciclo pudo cerrarse ya, y así sus archivos no esperan al ciclo siguiente.
        cerrados = _limpiar_data_cerrados(ruc_emisor, _docs_en_vuelo(ruc_emisor))
        if cerrados:
            logger.info("%d comprobante(s) cerrados; archivos de DATA eliminados.", cerrados)

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

    # Sin try/except KeyboardInterrupt: Python solo lo entrega al hilo principal.
    # El barrido periódico es la red de seguridad: recoge los CDR que llegaron a
    # medio escribir y los que watchdog no reportó (copias por red, reinicios).
    # Si no hay archivos nuevos, procesar_respuestas() sale de inmediato.
    while True:
        time.sleep(INTERVALO_BARRIDO_RPTA_SEG)
        procesar_respuestas()

# ---------------------------------------------------------------------------
# Main — lanza ambos hilos
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("  FACTURADOR SUNAT - SFS v2.1")
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

