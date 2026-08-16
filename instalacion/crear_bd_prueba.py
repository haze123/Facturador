"""
Crea una base de prueba con la estructura que necesita el daemon, sin datos.

Para que sirve: probar una instalacion nueva sin apuntar a la base de produccion.
Si dos daemons leen la misma base, el de prueba toma los comprobantes reales
pendientes, los manda a beta —donde no tienen validez fiscal—, recibe el CDR y
los marca enviado=true. El daemon real nunca los emitiria y en la base figurarian
como enviados. Es el peor error posible de este sistema y ocurre en silencio.

Solo LEE la base de origen: copia la estructura de las cuatro tablas que el
daemon consulta, mas los tipos enum que usan. No copia ningun dato.

Uso:
    python crear_bd_prueba.py <DATABASE_URL_origen> [nombre_bd_destino]

Imprime al final la DATABASE_URL que hay que darle a configurar.ps1.
"""
import sys
import urllib.parse

import psycopg2
import psycopg2.extensions

# Las unicas tablas que consulta el daemon (ver main.py). El resto del esquema de
# la aplicacion no hace falta para probarlo.
TABLAS = ("Cliente", "Configuracion", "Factura", "FacturaItem")


def _partes(url):
    """(dsn sin parametros de Prisma, esquema, nombre de la base)."""
    p = urllib.parse.urlsplit(url)
    consulta = urllib.parse.parse_qs(p.query)
    esquema = (consulta.get("schema") or ["public"])[0]
    limpia = urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", ""))
    return limpia, esquema, p.path.lstrip("/")


def _conectar(url, esquema="public"):
    dsn, _, _ = _partes(url)
    return psycopg2.connect(dsn, connect_timeout=15, options=f"-c search_path={esquema}")


def _enums_necesarios(cur, esquema):
    """Tipos enum que usan las columnas de TABLAS, con sus valores."""
    cur.execute(
        """
        SELECT DISTINCT t.typname,
               (SELECT string_agg(quote_literal(e.enumlabel), ',' ORDER BY e.enumsortorder)
                  FROM pg_enum e WHERE e.enumtypid = t.oid)
          FROM information_schema.columns c
          JOIN pg_type t ON t.typname = c.udt_name
         WHERE c.table_schema = %s AND c.table_name = ANY(%s) AND c.data_type = 'USER-DEFINED'
        """,
        (esquema, list(TABLAS)),
    )
    return cur.fetchall()


def _ddl_tabla(cur, esquema, tabla):
    """CREATE TABLE a partir de information_schema. Sin claves foraneas ni indices:
    el daemon no los necesita y arrastrarlos traeria medio esquema de la aplicacion."""
    cur.execute(
        """
        SELECT column_name, data_type, udt_name, is_nullable,
               character_maximum_length, numeric_precision, numeric_scale
          FROM information_schema.columns
         WHERE table_schema = %s AND table_name = %s
         ORDER BY ordinal_position
        """,
        (esquema, tabla),
    )
    columnas = []
    for nombre, tipo, udt, nulable, largo, precision, escala in cur.fetchall():
        if tipo == "USER-DEFINED":
            sql_tipo = f'"{udt}"'
        elif tipo == "character varying" and largo:
            sql_tipo = f"varchar({largo})"
        elif tipo == "numeric" and precision:
            sql_tipo = f"numeric({precision},{escala or 0})"
        elif tipo == "ARRAY":
            sql_tipo = udt.lstrip("_") + "[]"
        else:
            sql_tipo = tipo
        nulo = "" if nulable == "YES" else " NOT NULL"
        columnas.append(f'    "{nombre}" {sql_tipo}{nulo}')

    if not columnas:
        raise RuntimeError(f'la tabla "{tabla}" no existe en el origen')

    # La clave primaria si importa: la aplicacion y el daemon insertan por id.
    cur.execute(
        """
        SELECT kcu.column_name
          FROM information_schema.table_constraints tc
          JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
         WHERE tc.table_schema = %s AND tc.table_name = %s AND tc.constraint_type = 'PRIMARY KEY'
         ORDER BY kcu.ordinal_position
        """,
        (esquema, tabla),
    )
    pk = [f'"{f[0]}"' for f in cur.fetchall()]
    if pk:
        columnas.append(f"    PRIMARY KEY ({', '.join(pk)})")

    return f'CREATE TABLE "{tabla}" (\n' + ",\n".join(columnas) + "\n);"


def main(url_origen, nombre_destino):
    dsn_origen, esquema, bd_origen = _partes(url_origen)
    if nombre_destino == bd_origen:
        raise SystemExit("ERROR: la base de destino no puede ser la misma que la de origen")

    print(f"Origen : {bd_origen} (solo lectura)")
    print(f"Destino: {nombre_destino}")
    print()

    with _conectar(url_origen, esquema) as con, con.cursor() as cur:
        enums = _enums_necesarios(cur, esquema)
        ddls = [_ddl_tabla(cur, esquema, t) for t in TABLAS]
    print(f"Estructura leida: {len(TABLAS)} tablas, {len(enums)} tipo(s) enum")

    # CREATE DATABASE no puede ir dentro de una transaccion.
    con = _conectar(url_origen, esquema)
    con.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with con.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (nombre_destino,))
            if cur.fetchone():
                raise SystemExit(
                    f'ERROR: la base "{nombre_destino}" ya existe.\n'
                    f"       Borrarla a mano o elegir otro nombre."
                )
            cur.execute(f'CREATE DATABASE "{nombre_destino}"')
        print(f'Base "{nombre_destino}" creada')
    finally:
        con.close()

    # Ahora sobre la base nueva.
    partes = urllib.parse.urlsplit(dsn_origen)
    url_destino = urllib.parse.urlunsplit(
        (partes.scheme, partes.netloc, f"/{nombre_destino}", "", "")
    )
    con = psycopg2.connect(url_destino, connect_timeout=15)
    try:
        with con, con.cursor() as cur:
            for nombre, valores in enums:
                cur.execute(f'CREATE TYPE "{nombre}" AS ENUM ({valores})')
            for ddl in ddls:
                cur.execute(ddl)
        print(f"Estructura copiada: {len(enums)} enum(s) y {len(TABLAS)} tabla(s), sin datos")
    finally:
        con.close()

    print()
    print("Listo. Usar esta DATABASE_URL en configurar.ps1:")
    print(f"  {url_destino}?schema=public")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "facturador_prueba")
