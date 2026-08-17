"""
Adaptador SQL Server: el esquema Comprobantes / Items / Receptores.

Escrito contra una base real (AUXILIAR, un sistema de grifo) cuyos nombres de columna
coinciden casi uno a uno con el vocabulario del daemon. Sirve de plantilla: para un
cliente con otras tablas se copia este archivo y se le cambian las consultas.

Cuatro cosas que SQL Server no comparte con PostgreSQL, y que son las que hay que
mirar al adaptar:

  - Los marcadores son '?', no '%s'.
  - No hay booleano: 'enviado' es BIT, asi que "no enviado" es (=0 OR IS NULL).
  - No existe ANY(lista): el cierre de un resumen se arma con un IN (?,?,...).
  - No existe NULLS LAST: se ordena con un CASE que mande los nulos al final.
"""
import urllib.parse
from contextlib import closing

import pyodbc

# De mas nuevo a mas viejo: el 18 exige cifrado y hay que permitirle el certificado
# autofirmado que traen las instalaciones locales.
_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)

_SELECT_PENDIENTES = (
    "SELECT id,"
    "       tipo_comprobante,"
    "       NULL                            AS tipo_enum,"
    "       numeracion_comprobante,"
    "       fecha_emision,"
    "       tipo_moneda,"
    "       tipo_nota,"
    "       tipo_documento_afectado,"
    "       numeracion_documento_afectado,"
    "       motivo_documento_afectado,"
    "       gravadas, igv, total,"
    "       monto_letras"
    "  FROM dbo.Comprobantes"
    " WHERE (enviado = 0 OR enviado IS NULL)"
    " ORDER BY CASE WHEN fecha_emision IS NULL THEN 1 ELSE 0 END, fecha_emision ASC"
)


def _driver_disponible() -> str:
    instalados = set(pyodbc.drivers())
    for d in _DRIVERS:
        if d in instalados:
            return d
    raise RuntimeError(
        "No hay ningun driver ODBC de SQL Server instalado. Se descarga de Microsoft "
        "como 'ODBC Driver 18 for SQL Server'."
    )


def _cadena_odbc(url: str, timeout: int) -> str:
    """
    De sqlserver://usuario:clave@host:1433/base a una cadena ODBC.

    Sin usuario en la URL —o con ?trusted=yes— se usa autenticacion de Windows, que
    es lo habitual cuando el daemon corre en la misma PC que el servidor.
    """
    p = urllib.parse.urlsplit(url)
    opciones = urllib.parse.parse_qs(p.query)
    base = (p.path or "").lstrip("/")
    if not base:
        raise RuntimeError("Falta el nombre de la base en DATABASE_URL (sqlserver://host/BASE)")

    servidor = p.hostname or "localhost"
    if p.port:
        servidor = f"{servidor},{p.port}"

    partes = [
        f"DRIVER={{{_driver_disponible()}}}",
        f"SERVER={servidor}",
        f"DATABASE={base}",
        f"Connection Timeout={timeout}",
        "TrustServerCertificate=yes",
        # Sin MARS, ODBC admite un solo statement activo por conexion y el driver
        # responde "La conexion esta ocupada con los resultados de otro comando".
        # El daemon recorre los pendientes y adentro del bucle pide el receptor y
        # los items de cada uno, asi que necesita varios a la vez.
        "MARS_Connection=yes",
    ]
    confiada = (opciones.get("trusted") or ["no"])[0].lower() in ("yes", "true", "1", "si")
    if p.username and not confiada:
        partes.append(f"UID={urllib.parse.unquote(p.username)}")
        partes.append(f"PWD={urllib.parse.unquote(p.password or '')}")
    else:
        partes.append("Trusted_Connection=yes")
    return ";".join(partes) + ";"


def conectar(url: str, timeout: int):
    return pyodbc.connect(_cadena_odbc(url, timeout), timeout=timeout)


def _filas(conn, sql: str, params: tuple = ()) -> list:
    # El cursor se cierra siempre: si queda abierto con resultados sin leer, la
    # siguiente consulta sobre la misma conexion falla por ocupada.
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


def _marcas(cuantos: int) -> str:
    """(?,?,?) — SQL Server no tiene ANY(lista), hay que armar el IN a mano."""
    return "(" + ",".join("?" * cuantos) + ")"


def _texto(valor) -> str:
    return str(valor).strip() if valor is not None else ""


# --- lecturas ---------------------------------------------------------------

def reloj(conn) -> list:
    return _filas(conn, "SELECT SYSUTCDATETIME() AS utc, SYSDATETIME() AS con_zona")


def emisor(conn) -> str:
    filas = _filas(conn, "SELECT TOP 1 razon_social FROM dbo.Emisores")
    return filas[0]["razon_social"] if filas else ""


def receptor(conn, comprobante_id) -> dict:
    """Este esquema ya guarda tipo y numero de documento por separado."""
    filas = _filas(
        conn,
        "SELECT r.tipo_documento, r.numero_documento, r.razon_social"
        "  FROM dbo.Comprobantes c JOIN dbo.Receptores r ON r.id = c.ReceptorId"
        " WHERE c.id = ?",
        (comprobante_id,),
    )
    if not filas:
        return {}
    r = filas[0]
    if not _texto(r["numero_documento"]):
        return {}
    return {
        "tipo_documento":   _texto(r["tipo_documento"]),
        "numero_documento": _texto(r["numero_documento"]),
        "razon_social":     _texto(r["razon_social"]),
    }


def items(conn, comprobante_id) -> list:
    return _filas(
        conn,
        "SELECT descripcion,"
        "       cantidad,"
        "       precio            AS precio_unit,"
        "       dec_total         AS total,"
        "       codigo_producto,"
        "       dec_cantidad,"
        "       valor,"
        "       igv_venta,"
        "       precio,"
        "       medida"
        "  FROM dbo.Items WHERE ComprobanteId = ? ORDER BY id",
        (comprobante_id,),
    )


def pendientes(conn) -> list:
    return _filas(conn, _SELECT_PENDIENTES)


# --- escrituras -------------------------------------------------------------

def marcar_enviados(conn, numeraciones: list, limpiar_error: bool = True) -> int:
    if not numeraciones:
        return 0
    nums = list(numeraciones)
    extra = ", errors=NULL" if limpiar_error else ""
    return _escribir(
        conn,
        f"UPDATE dbo.Comprobantes SET enviado=1{extra} "
        f" WHERE numeracion_comprobante IN {_marcas(len(nums))}",
        tuple(nums),
    )


def marcar_enviado(conn, numeracion: str, enviado: bool = True,
                   limpiar_error: bool = True) -> int:
    extra = ", errors=NULL" if limpiar_error else ""
    return _escribir(
        conn,
        f"UPDATE dbo.Comprobantes SET enviado=?{extra} WHERE numeracion_comprobante=?",
        (1 if enviado else 0, numeracion),
    )


def guardar_error(conn, numeracion: str, detalle: str) -> int:
    return _escribir(
        conn,
        "UPDATE dbo.Comprobantes SET errors=? WHERE numeracion_comprobante=?",
        (detalle, numeracion),
    )


def guardar_error_varios(conn, numeraciones: list, detalle: str) -> int:
    if not numeraciones:
        return 0
    nums = list(numeraciones)
    return _escribir(
        conn,
        f"UPDATE dbo.Comprobantes SET errors=? "
        f" WHERE numeracion_comprobante IN {_marcas(len(nums))}",
        (detalle, *nums),
    )
