"""
De donde salen los comprobantes: cada motor de base de datos, un adaptador.

El daemon no sabe con que esta hablando. Pide siempre lo mismo —los pendientes, el
receptor, los items— y recibe diccionarios con el mismo vocabulario, venga de
PostgreSQL o de SQL Server. Lo que cambia de un cliente a otro (nombres de tablas,
de columnas, dialecto de SQL) vive dentro del adaptador y en ningun otro lado.

El motor se deduce del esquema de DATABASE_URL, asi no hay una segunda variable que
pueda quedar en desacuerdo con la primera:

    postgresql://usuario:clave@host:5432/base?schema=public
    sqlserver://usuario:clave@host:1433/base
    sqlserver://host:1433/base?trusted=yes          (autenticacion de Windows)

Para agregar un cliente con su propio esquema se copia el adaptador mas parecido y
se le cambian las consultas. NO se arma un mapeador generico configurable: cinco
archivos de sesenta lineas, cada uno con su SQL a la vista, se depuran; un mapeo
indirecto hay que descifrarlo justo cuando algo esta fallando.
"""
import urllib.parse

_MOTORES = {
    "postgres":   "postgres",
    "postgresql": "postgres",
    "sqlserver":  "sqlserver",
    "mssql":      "sqlserver",
}


def motor_de(url: str) -> str:
    """Nombre del motor segun el esquema de la URL."""
    esquema = urllib.parse.urlsplit(url or "").scheme.lower()
    if esquema not in _MOTORES:
        conocidos = ", ".join(sorted(set(_MOTORES)))
        raise RuntimeError(
            f"No se reconoce el motor de base de datos en DATABASE_URL "
            f"(esquema '{esquema or 'vacio'}'). Esperado uno de: {conocidos}."
        )
    return _MOTORES[esquema]


def elegir(url: str):
    """El modulo adaptador que corresponde a esa URL."""
    nombre = motor_de(url)
    if nombre == "postgres":
        from . import postgres as impl
    else:
        from . import sqlserver as impl
    return impl


def url_sin_clave(url: str) -> str:
    """La URL sin la contrasena, para poder mostrarla en el log."""
    import re
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url or "") or "(sin configurar)"
