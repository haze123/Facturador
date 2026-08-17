"""
Adaptador PostgreSQL: el esquema de la aplicacion (Prisma).

Las tablas son Factura / FacturaItem / Cliente / Configuracion, con columnas en
camelCase y entrecomilladas, que es como las genera Prisma. Todo lo que sale de aca
va traducido al vocabulario del daemon (snake_case) para que main.py no tenga que
saber de que motor vino.
"""
import urllib.parse
from contextlib import closing

import psycopg2

# Columnas del comprobante, ya con el nombre que usa el daemon.
_SELECT_PENDIENTES = (
    'SELECT id,'
    '       "tipoComprobante"               AS tipo_comprobante,'
    '       tipo::text                      AS tipo_enum,'
    '       "numeracionComprobante"         AS numeracion_comprobante,'
    '       "fechaEmision"                  AS fecha_emision,'
    '       "tipoMoneda"                    AS tipo_moneda,'
    '       "tipoNota"                      AS tipo_nota,'
    '       "tipoDocumentoAfectado"         AS tipo_documento_afectado,'
    '       "numeracionDocumentoAfectado"   AS numeracion_documento_afectado,'
    '       "motivoDocumentoAfectado"       AS motivo_documento_afectado,'
    '       gravadas, igv, total,'
    '       "montoLetras"                   AS monto_letras'
    '  FROM public."Factura"'
    ' WHERE enviado IS NOT TRUE'
    ' ORDER BY "fechaEmision" ASC NULLS LAST'
)


def _dsn_desde_url(url: str):
    """
    Separa la URL de Prisma en algo que psycopg2 entienda.

    Se usa la misma DATABASE_URL que la aplicacion para no declarar la conexion dos
    veces, pero trae parametros propios de Prisma —?schema, connection_limit— que
    psycopg2 rechaza. El schema se traduce a search_path y el resto se descarta.
    """
    partes = urllib.parse.urlsplit(url)
    consulta = urllib.parse.parse_qs(partes.query)
    esquema = (consulta.get("schema") or ["public"])[0]
    limpia = urllib.parse.urlunsplit((partes.scheme, partes.netloc, partes.path, "", ""))
    return limpia, f"-c search_path={esquema}"


def conectar(url: str, timeout: int):
    dsn, opciones = _dsn_desde_url(url)
    return psycopg2.connect(dsn, connect_timeout=timeout, options=opciones)


def _filas(conn, sql: str, params: tuple = ()) -> list:
    with closing(conn.cursor()) as cur:
        cur.execute(sql, params)
        columnas = [d[0] for d in cur.description]
        return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def _escribir(conn, sql: str, params: tuple) -> int:
    with closing(conn.cursor()) as cur:
        cur.execute(sql, params)
        filas = cur.rowcount
    conn.commit()
    return filas


# --- lecturas ---------------------------------------------------------------

def reloj(conn) -> list:
    """El reloj del servidor de BD, para medir su desfase con la hora local."""
    return _filas(conn, "SELECT now() AT TIME ZONE 'utc' AS utc, now() AS con_zona")


def emisor(conn) -> str:
    """
    Razon social del emisor. La aplicacion no tiene tabla de emisores: solo guarda
    el nombre en Configuracion, y el RUC sale de EMISOR_RUC en el .env.
    """
    filas = _filas(conn, 'SELECT "nombreEmpresa" FROM public."Configuracion" LIMIT 1')
    return filas[0]["nombreEmpresa"] if filas else ""


def _texto(valor) -> str:
    return str(valor).strip() if valor is not None else ""


def receptor(conn, comprobante_id) -> dict:
    """
    Receptor del comprobante, en el vocabulario del daemon.

    Cliente no guarda tipo ni numero de documento por separado, asi que se deducen
    aca —es una particularidad de este esquema, no una regla general—: con RUC es una
    empresa (catalogo 06, tipo '6') y con DNI una persona (tipo '1'). Sin ninguno de
    los dos queda como consumidor final, que es lo correcto para una boleta de
    mostrador.
    """
    filas = _filas(
        conn,
        'SELECT c.dni, c.ruc, c."razonSocial" AS razon_social, c.nombre '
        '  FROM public."Factura" f JOIN public."Cliente" c ON c.id = f."clienteId" '
        ' WHERE f.id = %s',
        (comprobante_id,),
    )
    if not filas:
        return {}
    cli = filas[0]
    ruc, dni = _texto(cli["ruc"]), _texto(cli["dni"])
    if ruc:
        return {"tipo_documento": "6", "numero_documento": ruc,
                "razon_social": _texto(cli["razon_social"]) or _texto(cli["nombre"])}
    if dni:
        return {"tipo_documento": "1", "numero_documento": dni,
                "razon_social": _texto(cli["nombre"])}
    return {}


def items(conn, comprobante_id) -> list:
    """
    Items del comprobante. La aplicacion llena nombre, cantidad, precioUnit y total;
    las columnas del desglose de IGV existen pero suelen venir vacias, y main.py las
    calcula cuando faltan.
    """
    return _filas(
        conn,
        'SELECT nombre                AS descripcion,'
        '       cantidad,'
        '       "precioUnit"          AS precio_unit,'
        '       total,'
        '       "codigoProducto"      AS codigo_producto,'
        '       "decCantidad"         AS dec_cantidad,'
        '       valor,'
        '       "igvVenta"            AS igv_venta,'
        '       precio'
        '  FROM public."FacturaItem" WHERE "facturaId" = %s ORDER BY id',
        (comprobante_id,),
    )


def pendientes(conn) -> list:
    """
    Todo lo que no se envio todavia, sin distinguir tipo: main.py separa los que van
    de a uno de las boletas que van en el resumen diario.

    Las filas sin numeracion NO se filtran: una venta cobrada a la que la aplicacion
    nunca le asigno numero igual no se puede emitir, pero descartarla aca la hacia
    desaparecer sin dejar una linea en el log.
    """
    return _filas(conn, _SELECT_PENDIENTES)


# --- escrituras -------------------------------------------------------------

def marcar_enviados(conn, numeraciones: list, limpiar_error: bool = True) -> int:
    """Cierra varios comprobantes de una vez (el fan-out de un resumen diario)."""
    if not numeraciones:
        return 0
    extra = ", errors=NULL" if limpiar_error else ""
    return _escribir(
        conn,
        f'UPDATE public."Factura" SET enviado=%s{extra} '
        f' WHERE "numeracionComprobante" = ANY(%s)',
        (True, list(numeraciones)),
    )


def marcar_enviado(conn, numeracion: str, enviado: bool = True,
                   limpiar_error: bool = True) -> int:
    extra = ", errors=NULL" if limpiar_error else ""
    return _escribir(
        conn,
        f'UPDATE public."Factura" SET enviado=%s{extra} WHERE "numeracionComprobante"=%s',
        (enviado, numeracion),
    )


def guardar_error(conn, numeracion: str, detalle: str) -> int:
    return _escribir(
        conn,
        'UPDATE public."Factura" SET errors=%s WHERE "numeracionComprobante"=%s',
        (detalle, numeracion),
    )


def guardar_error_varios(conn, numeraciones: list, detalle: str) -> int:
    if not numeraciones:
        return 0
    return _escribir(
        conn,
        'UPDATE public."Factura" SET errors=%s WHERE "numeracionComprobante" = ANY(%s)',
        (detalle, list(numeraciones)),
    )
