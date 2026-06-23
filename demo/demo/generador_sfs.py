import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

import pandas as pd
import pyodbc
from dotenv import load_dotenv

# Carga .env desde el mismo directorio que este archivo
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------------------------------------------------------------------------
# Configuración (leída desde .env o variables de entorno)
# ---------------------------------------------------------------------------

_BASE = os.path.dirname(os.path.abspath(__file__))

_SFS_DATA = os.getenv("SFS_DATA_DIR", r"C:\SFS_v2.1\sunat_archivos\sfs\DATA")
FACTURADOR_SUNAT_IMPORT_DIR = (
    _SFS_DATA if os.path.exists(_SFS_DATA)
    else os.path.join(_BASE, "sunat_archivos", "DATA")
)

DB_CONFIG = {
    "driver": "{SQL Server}",
    "server":   os.getenv("DB_SERVER",   r".\SQLEXPRESS"),
    "database": os.getenv("DB_DATABASE", "AUXILIAR"),
    "trusted_connection": "yes",
    "timeout": 30,
}

SFS_BD_PATH      = os.getenv("SFS_BD_PATH",  r"C:\SFS_v-2.1\bd\BDFacturador.db")
SFS_BASE_URL     = os.getenv("SFS_BASE_URL", "http://localhost:9000")
_SFS_RPTA        = os.getenv("SFS_RPTA_DIR", r"C:\SFS_v2.1\sunat_archivos\sfs\RPTA")
SFS_RPTA_DIR     = (
    _SFS_RPTA if os.path.exists(_SFS_RPTA)
    else os.path.join(_BASE, "sunat_archivos", "RPTA")
)
_ESTADOS_ERROR   = ('05', '10')   # Rechazado / Error SFS → se reintenta
_TIPOS_SFS       = {'01', '03', '07', '08', 'RC', 'RA'}  # Tipos soportados por el SFS
EMISOR_RUC_OVERRRIDE = os.getenv("EMISOR_RUC", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utilidades generales
# ---------------------------------------------------------------------------

def conectar_bd():
    cfg = DB_CONFIG
    conn_str = (
        f"DRIVER={cfg['driver']};SERVER={cfg['server']};"
        f"DATABASE={cfg['database']};Trusted_Connection={cfg['trusted_connection']};"
    )
    return pyodbc.connect(conn_str, timeout=cfg['timeout'])


def limpiar_nombre_archivo(texto):
    return re.sub(r'[<>:"/\\|?*\n\r\t]+', '_', str(texto or "").strip())[:250]


def formatear_decimal(valor):
    if valor is None:
        return Decimal("0.00")
    try:
        if pd.isna(valor):
            return Decimal("0.00")
    except (TypeError, ValueError):
        pass
    try:
        return Decimal(str(valor)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0.00")


def formatear_fecha_hora(fecha_raw):
    if isinstance(fecha_raw, datetime):
        return fecha_raw
    if isinstance(fecha_raw, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
            try:
                return datetime.strptime(fecha_raw.strip(), fmt)
            except ValueError:
                pass
    raise ValueError(f"Fecha inválida: {fecha_raw!r}")


def numero_a_letras(monto, moneda='PEN'):
    UNIDADES = ['CERO','UN','DOS','TRES','CUATRO','CINCO','SEIS','SIETE','OCHO','NUEVE',
                'DIEZ','ONCE','DOCE','TRECE','CATORCE','QUINCE','DIECISEIS','DIECISIETE',
                'DIECIOCHO','DIECINUEVE']
    DECENAS  = ['','','VEINTE','TREINTA','CUARENTA','CINCUENTA','SESENTA','SETENTA','OCHENTA','NOVENTA']
    CENTENAS = ['','CIENTO','DOSCIENTOS','TRESCIENTOS','CUATROCIENTOS','QUINIENTOS',
                'SEISCIENTOS','SETECIENTOS','OCHOCIENTOS','NOVECIENTOS']
    MONEDAS  = {'PEN': 'SOLES', 'USD': 'DOLARES AMERICANOS', 'EUR': 'EUROS'}

    def _grupo(n):
        if n < 20:
            return UNIDADES[n]
        d, u = divmod(n, 10)
        base = ('VEINTI' + UNIDADES[u]) if (d == 2 and u) else DECENAS[d]
        return base + ((' Y ' + UNIDADES[u]) if (d != 2 and u) else '')

    def _cientos(n):
        if n == 100:
            return 'CIEN'
        c, r = divmod(n, 100)
        return CENTENAS[c] + (' ' + _grupo(r) if r else '')

    def _entero(n):
        if n == 0:   return 'CERO'
        if n < 100:  return _grupo(n)
        if n < 1000: return _cientos(n)
        m, r = divmod(n, 1000)
        mil = 'MIL' if m == 1 else ((_grupo(m) if m < 100 else _cientos(m)) + ' MIL')
        return mil + (' ' + (_cientos(r) if r >= 100 else _grupo(r)) if r else '')

    f = float(monto)
    entero, centavos = int(f), round((f - int(f)) * 100)
    nombre_mon = MONEDAS.get(str(moneda).upper(), str(moneda).upper())
    return f'SON {_entero(entero)} CON {centavos:02d}/100 {nombre_mon}'


def escribir_archivo(ruta, contenido):
    tmp = ruta + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='') as fh:
        fh.write(contenido)
    os.replace(tmp, ruta)


# ---------------------------------------------------------------------------
# Acceso a SQL Server
# ---------------------------------------------------------------------------

def obtener_emisor(conn):
    df = pd.read_sql("SELECT TOP 1 ruc, razon_social FROM Emisores", conn)
    return df.iloc[0].to_dict() if not df.empty else None


def obtener_receptor(conn, receptor_id):
    if receptor_id is None or pd.isna(receptor_id):
        return {}
    df = pd.read_sql("SELECT TOP 1 * FROM Receptores WHERE id = ?", conn, params=(int(receptor_id),))
    return df.iloc[0].to_dict() if not df.empty else {}


def obtener_items(conn, comprobante_id):
    return pd.read_sql("SELECT * FROM Items WHERE ComprobanteId = ?", conn, params=(int(comprobante_id),))


def obtener_comprobantes(conn, only_pending=True):
    filtro = "WHERE enviado IS NULL OR enviado = 0" if only_pending else ""
    return pd.read_sql(f"SELECT * FROM Comprobantes {filtro} ORDER BY fecha_emision DESC", conn)


# ---------------------------------------------------------------------------
# Generación de archivos SFS
# ---------------------------------------------------------------------------

def _nombre_base(ruc, tipo, num):
    num = str(num or "").strip()
    serie, corr = num.split('-', 1) if '-' in num else ("0000", num or "00000000")
    return limpiar_nombre_archivo(f"{ruc}-{tipo}-{serie}-{corr}")


def procesar_comprobante(conn, cursor, comp, ruc_emisor):
    comp_id   = comp['id']
    num_comp  = str(comp.get('numeracion_comprobante', '')).strip()
    tipo_comp = str(comp.get('tipo_comprobante', '')).strip() or '01'
    base      = _nombre_base(ruc_emisor, tipo_comp, num_comp)
    data_dir  = FACTURADOR_SUNAT_IMPORT_DIR
    os.makedirs(data_dir, exist_ok=True)

    rutas = {e: os.path.join(data_dir, f"{base}.{e}") for e in ('cab', 'det', 'tri', 'ley', 'PAG')}

    enviado  = comp.get('enviado')
    faltan   = any(not os.path.exists(rutas[e]) for e in ('cab', 'det', 'tri', 'ley'))
    if not (pd.isna(enviado) or enviado == 0 or faltan):
        return False

    receptor     = obtener_receptor(conn, comp.get('ReceptorId'))
    tipo_doc_rec = str(receptor.get('tipo_documento',   '0')).strip() or '0'
    num_doc_rec  = str(receptor.get('numero_documento', '00000000')).strip() or '00000000'
    razon_social = str(receptor.get('razon_social', 'CLIENTE VARIOS')).strip() or 'CLIENTE VARIOS'
    moneda       = str(comp.get('tipo_moneda', 'PEN')).strip() or 'PEN'

    fecha_dt  = formatear_fecha_hora(comp.get('fecha_emision'))
    fecha_str = fecha_dt.strftime('%Y-%m-%d')
    hora_str  = fecha_dt.strftime('%H:%M:%S')

    # Calcular totales desde ítems (evita error SUNAT 3291/3277)
    df_items   = obtener_items(conn, comp_id)
    lineas_det = []
    tot_grav   = Decimal("0.00")
    tot_igv    = Decimal("0.00")

    for _, item in df_items.iterrows():
        cant      = formatear_decimal(item.get('cantidad', 1))
        desc      = str(item.get('descripcion', 'ITEM')).replace('|', ' ').strip() or 'ITEM'
        v_unit_db = formatear_decimal(item.get('valor_unitario', 0))
        v_vta_db  = formatear_decimal(item.get('valor_venta',    0))
        codigo    = str(item.get('codigo_producto', '-')).strip() or '-'
        medida    = str(item.get('medida', 'NIU')).strip() or 'NIU'

        # Si valor_venta ≈ valor_unitario, el campo almacena precio unitario
        if cant > 0 and abs(float(v_vta_db) - float(v_unit_db)) < 0.01:
            v_vta = (v_vta_db * cant).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            v_vta = v_vta_db

        v_unit = (v_vta / cant).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP) if cant > 0 else v_unit_db
        igv_it = (v_vta  * Decimal("0.18")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        p_unit = (v_unit * Decimal("1.18")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        tot_grav += v_vta
        tot_igv  += igv_it

        lineas_det.append(
            f"{medida}|{cant:.2f}|{codigo}|-|{desc}|{v_unit:.6f}|"
            f"{igv_it:.2f}|1000|{igv_it:.2f}|{v_vta:.2f}|IGV|VAT|10|18.00|"
            "-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|-|"
            f"{p_unit:.2f}|{v_vta:.2f}|0.00|\n"
        )

    if lineas_det:
        tot_grav = tot_grav.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tot_igv  = tot_igv.quantize( Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        logger.warning("Comprobante %s sin ítems; .det omitido.", num_comp)
        tot_grav = formatear_decimal(comp.get('total_gravadas', 0))
        tot_igv  = formatear_decimal(comp.get('total_igv',      0))

    tot_venta = (tot_grav + tot_igv).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    ml_raw = comp.get('monto_letras')
    monto_letras = (numero_a_letras(tot_venta, moneda)
                    if ml_raw is None or (not isinstance(ml_raw, str) and pd.isna(ml_raw))
                    else str(ml_raw).strip() or numero_a_letras(tot_venta, moneda))

    escribir_archivo(rutas['cab'],
        f"0101|{fecha_str}|{hora_str}|-|0000|{tipo_doc_rec}|{num_doc_rec}|"
        f"{razon_social}|{moneda}|{tot_igv:.2f}|{tot_grav:.2f}|{tot_venta:.2f}|"
        f"0.00|0.00|0.00|{tot_venta:.2f}|2.1|2.0|\n"
    )
    escribir_archivo(rutas['PAG'], f"Contado|{tot_venta:.2f}|{moneda}|\n")
    escribir_archivo(rutas['tri'], f"1000|IGV|VAT|{tot_grav:.2f}|{tot_igv:.2f}|\n")
    escribir_archivo(rutas['ley'], f"1000|{monto_letras}|\n")

    if lineas_det:
        escribir_archivo(rutas['det'], ''.join(lineas_det))
    elif os.path.exists(rutas['det']):
        os.remove(rutas['det'])

    cursor.execute("UPDATE Comprobantes SET enviado = 1 WHERE id = ?", (int(comp_id),))
    conn.commit()
    logger.info("Archivos SFS generados: %s", num_comp)
    return True


# ---------------------------------------------------------------------------
# SFS REST API
# ---------------------------------------------------------------------------

def _sfs_post(path, payload):
    url = f"{SFS_BASE_URL}/{path}"
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST"
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


def _registrar_en_sfs_bd(ruc_emisor, docs):
    """Inserta en SFS BD los docs procesados que el SFS no registra (ej: facturas aceptadas al instante)."""
    if not os.path.exists(SFS_BD_PATH):
        return
    time.sleep(2)  # dar tiempo al SFS para actualizar DOCUMENTO
    with sqlite3.connect(SFS_BD_PATH) as sfs:
        for doc in docs:
            tip = str(doc.get("tip_docu", "")).strip()
            if tip not in _TIPOS_SFS:
                continue
            num  = str(doc.get("num_docu", "")).strip()
            arch = f"{ruc_emisor}-{tip}-{num}"
            existe = sfs.execute(
                "SELECT 1 FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                (ruc_emisor, tip, num)
            ).fetchone()
            if not existe:
                sfs.execute(
                    "INSERT INTO DOCUMENTO (NUM_RUC, TIP_DOCU, NUM_DOCU, NOM_ARCH, IND_SITU, DES_OBSE) "
                    "VALUES (?,?,?,?,?,?)",
                    (ruc_emisor, tip, num, arch, '03', 'Aceptado por SUNAT')
                )
                logger.debug("Registrado en SFS BD: %s-%s", tip, num)


def _tiene_cdr(ruc, tip, num):
    """True si ya existe un CDR (en RPTA o procesados) para este documento."""
    nombre = f"R{ruc}-{tip}-{num}.zip"
    return (
        os.path.exists(os.path.join(SFS_RPTA_DIR, nombre)) or
        os.path.exists(os.path.join(SFS_RPTA_DIR, "procesados", nombre))
    )


def _eliminar_data_files(nom_arch):
    """Elimina los DATA files de un documento para que el SFS no lo recargue."""
    for ext in ('cab', 'det', 'tri', 'ley', 'PAG', 'RDI', 'TRD', 'DET'):
        ruta = os.path.join(FACTURADOR_SUNAT_IMPORT_DIR, f"{nom_arch}.{ext}")
        if os.path.exists(ruta):
            try:
                os.remove(ruta)
            except OSError:
                pass


def _activar_pendientes_sfs_bd(ruc_emisor, ya_procesados):
    """Busca docs en SFS BD con IND_SITU='01'/'02' que no tienen CDR y los activa.
    Docs con CDR ya procesado: corrige a '03' y elimina DATA files para evitar re-carga."""
    if not os.path.exists(SFS_BD_PATH):
        return
    ya_keys = {(d["tip_docu"], d["num_docu"]) for d in ya_procesados}
    with sqlite3.connect(SFS_BD_PATH) as sfs:
        rows = sfs.execute(
            "SELECT TIP_DOCU, NUM_DOCU, NOM_ARCH FROM DOCUMENTO "
            "WHERE NUM_RUC=? AND TIP_DOCU IN ('01','03','07','08') AND IND_SITU IN ('01','02')",
            (ruc_emisor,)
        ).fetchall()
        docs_extra = []
        for tip, num, nom_arch in rows:
            if (tip, num) in ya_keys:
                continue
            if _tiene_cdr(ruc_emisor, tip, num):
                # Ya aceptado — corregir estado y limpiar DATA files para evitar re-carga por el SFS
                sfs.execute(
                    "UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE='Aceptado (CDR procesado)' "
                    "WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? AND IND_SITU IN ('01','02')",
                    (ruc_emisor, tip, num)
                )
                _eliminar_data_files(nom_arch or f"{ruc_emisor}-{tip}-{num}")
                logger.debug("Corregido a 03 y DATA eliminados: %s-%s", tip, num)
                continue
            docs_extra.append({"num_ruc": ruc_emisor, "tip_docu": tip, "num_docu": num})
    if docs_extra:
        logger.info("%d doc(s) en SFS BD sin CDR pendientes de activar.", len(docs_extra))
        activar_procesamiento_sfs(docs_extra)


def activar_procesamiento_sfs(documentos):
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
            logger.info("[SFS] Tipo %s no soportado por SFS, omitido: %s", tip, label)
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
            # Reintentar una vez con delay adicional
            time.sleep(3)
            r2 = _sfs_post("api/enviarXML.htm", payload)
            if r2 and r2.get("validacion") == "EXITO":
                logger.info("[SFS] Enviado a SUNAT (reintento): %s", label)
            else:
                logger.warning("[SFS] Error al enviar %s: %s", label, r2)
        time.sleep(1)


# ---------------------------------------------------------------------------
# RC para boletas con más de 5 días
# ---------------------------------------------------------------------------

def generar_rc_boletas_viejas(ruc_emisor):
    if not os.path.exists(SFS_BD_PATH):
        return

    with sqlite3.connect(SFS_BD_PATH) as sfs:
        rows = sfs.execute(
            "SELECT NUM_DOCU, NOM_ARCH FROM DOCUMENTO "
            "WHERE NUM_RUC=? AND TIP_DOCU='03' AND IND_SITU='06' "
            "AND (DES_OBSE LIKE '%mas de 5%' OR DES_OBSE LIKE '%más de 5%')",
            (ruc_emisor,)
        ).fetchall()

    if not rows:
        return

    data_dir         = FACTURADOR_SUNAT_IMPORT_DIR
    boletas_por_fecha = {}

    for num_docu, nom_arch in rows:
        if nom_arch:
            base = os.path.join(data_dir, nom_arch)
        else:
            partes = num_docu.split('-') if '-' in num_docu else ['B001', num_docu]
            serie  = partes[0] if len(partes) >= 2 else 'B001'
            corr   = partes[1] if len(partes) >= 2 else num_docu
            base   = os.path.join(data_dir, f"{ruc_emisor}-03-{serie}-{corr}")

        if not (os.path.exists(base + '.cab') and os.path.exists(base + '.tri')):
            logger.warning("Faltan archivos para boleta %s; omitida del RC.", num_docu)
            continue

        cab_p = open(base + '.cab', encoding='utf-8').readline().strip().split('|')
        tri_p = open(base + '.tri', encoding='utf-8').readline().strip().split('|')

        if len(cab_p) < 12 or len(tri_p) < 5:
            logger.warning("Formato incompleto en boleta %s; omitida.", num_docu)
            continue

        fec   = cab_p[1]
        b_imp = tri_p[3]
        igv   = tri_p[4]
        try:
            tot = str((Decimal(b_imp) + Decimal(igv)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
        except Exception:
            tot = cab_p[11] if len(cab_p) > 11 else "0.00"

        boletas_por_fecha.setdefault(fec, []).append({
            'num': num_docu, 'tip': cab_p[5] or '0', 'doc': cab_p[6] or '-',
            'base': b_imp, 'igv': igv, 'tot': tot,
        })

    # Aplanar todas las boletas en una lista única (un solo RC independiente de la fecha)
    todas_boletas = []
    for fec in sorted(boletas_por_fecha):
        for b in boletas_por_fecha[fec]:
            b['fec'] = fec
            todas_boletas.append(b)

    if not todas_boletas:
        return

    today_str = datetime.now().strftime('%Y%m%d')
    today_iso = datetime.now().strftime('%Y-%m-%d')

    with sqlite3.connect(SFS_BD_PATH) as sfs:
        seq = len(sfs.execute(
            "SELECT 1 FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='RC' AND NUM_DOCU LIKE ?",
            (ruc_emisor, f"RC-{today_str}-%")
        ).fetchall()) + 1

        rc_num  = f"RC-{today_str}-{seq}"
        rc_arch = f"{ruc_emisor}-{rc_num}"

        rdi = '\n'.join(
            f"{b['fec']}|{today_iso}|03|{b['num']}|{b['tip']}|{b['doc']}|"
            f"PEN|{b['base']}|0.00|0.00|0.00|0.00|0.00|{b['tot']}|-|-|-|-|-|-|-|-|1|"
            for b in todas_boletas
        ) + '\n'
        trd = '\n'.join(
            f"{i}|1000|IGV|VAT|{b['base']}|{b['igv']}"
            for i, b in enumerate(todas_boletas, 1)
        ) + '\n'
        det = '\n'.join(
            f"03|{b['num']}|{b['tip']}|{b['doc']}|"
            f"PEN|{b['base']}|0.00|0.00|0.00|0.00|0.00|{b['tot']}|-|-|-|-|-|-|-|-|1|-|-|"
            for b in todas_boletas
        ) + '\n'

        for ext, content in (
            ('.cab', f"{rc_num}|{today_iso}|{today_iso}|\n"),
            ('.RDI', rdi), ('.TRD', trd), ('.DET', det),
        ):
            with open(os.path.join(data_dir, f"{rc_arch}{ext}"), 'w', encoding='utf-8') as fh:
                fh.write(content)

        sfs.execute(
            "DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU='RC' AND NUM_DOCU=?",
            (ruc_emisor, rc_num)
        )
        sfs.execute(
            "INSERT INTO DOCUMENTO (NUM_RUC, TIP_DOCU, NUM_DOCU, NOM_ARCH, IND_SITU, TIP_ARCH) VALUES (?,?,?,?,?,?)",
            (ruc_emisor, 'RC', rc_num, rc_arch, '01', 'TXT')
        )
        # Marcar boletas cubiertas como '03' y limpiar sus DATA files
        for b in todas_boletas:
            sfs.execute(
                "UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE=? "
                "WHERE NUM_RUC=? AND TIP_DOCU='03' AND NUM_DOCU=?",
                (f"Aceptado via {rc_num}", ruc_emisor, b['num'])
            )
            serie, corr = (b['num'].split('-', 1) + [''])[:2]
            nom_boleta = f"{ruc_emisor}-03-{serie}-{corr}" if corr else f"{ruc_emisor}-03-{b['num']}"
            _eliminar_data_files(nom_boleta)
        docs_rc = [{'num_ruc': ruc_emisor, 'tip_docu': 'RC', 'num_docu': rc_num}]
        logger.info("RC %s generado: %d boleta(s)", rc_num, len(todas_boletas))

    if docs_rc:
        activar_procesamiento_sfs(docs_rc)


# ---------------------------------------------------------------------------
# Flujo principal
# ---------------------------------------------------------------------------

def resetear_rechazados_en_sfs_bd(conn_aux, ruc_emisor):
    if not os.path.exists(SFS_BD_PATH):
        return
    ph = ','.join('?' * len(_ESTADOS_ERROR))
    with sqlite3.connect(SFS_BD_PATH) as sfs:
        rows = sfs.execute(
            f"SELECT NUM_DOCU, TIP_DOCU FROM DOCUMENTO WHERE NUM_RUC=? AND IND_SITU IN ({ph})",
            (ruc_emisor, *_ESTADOS_ERROR)
        ).fetchall()
        if not rows:
            return
        cur = conn_aux.cursor()
        for num_docu, tip_docu in rows:
            cur.execute(
                "UPDATE Comprobantes SET enviado=0 WHERE numeracion_comprobante=? AND tipo_comprobante=?",
                (num_docu, tip_docu)
            )
        sfs.execute(
            f"DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND IND_SITU IN ({ph})",
            (ruc_emisor, *_ESTADOS_ERROR)
        )
    conn_aux.commit()
    logger.info("Reseteados %d comprobantes rechazados para reenvío.", len(rows))


def generar_archivos_sfs(only_pending=True):
    conn = None
    try:
        conn = conectar_bd()
        emisor = obtener_emisor(conn)
        if not emisor:
            logger.error("No se encontró información del Emisor.")
            return

        ruc_emisor = EMISOR_RUC_OVERRRIDE or str(emisor.get('ruc', '')).strip() or '00000000000'

        if only_pending:
            resetear_rechazados_en_sfs_bd(conn, ruc_emisor)

        df_comp = obtener_comprobantes(conn, only_pending)

        if df_comp.empty:
            logger.info("No hay comprobantes pendientes en SQL Server.")
            if only_pending:
                _activar_pendientes_sfs_bd(ruc_emisor, [])
                generar_rc_boletas_viejas(ruc_emisor)
            return

        cursor        = conn.cursor()
        docs_generados = []
        for _, comp in df_comp.iterrows():
            try:
                if procesar_comprobante(conn, cursor, comp, ruc_emisor):
                    docs_generados.append({
                        "num_ruc":  ruc_emisor,
                        "tip_docu": str(comp.get('tipo_comprobante', '')).strip().zfill(2),
                        "num_docu": str(comp.get('numeracion_comprobante', '')).strip(),
                    })
            except Exception:
                logger.exception("Error procesando %r", comp.get('numeracion_comprobante'))

        if docs_generados:
            logger.info("%d comprobante(s) generados.", len(docs_generados))
            activar_procesamiento_sfs(docs_generados)
            _registrar_en_sfs_bd(ruc_emisor, docs_generados)
        else:
            logger.info("Todos los comprobantes ya están al día.")

        if only_pending:
            _activar_pendientes_sfs_bd(ruc_emisor, docs_generados)
            generar_rc_boletas_viejas(ruc_emisor)

    except Exception:
        logger.exception("Error en generación SFS")
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    generar_archivos_sfs()
