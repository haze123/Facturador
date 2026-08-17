"""
El archivo de datos de un cliente: leerlo, validarlo y comparar contra lo instalado.

La idea es tener por cada cliente un archivo de texto con sus datos, para no
depender de que alguien se acuerde del ubigeo cuando hay que reinstalar. El
instalador lo lee, compara contra lo que el SFS tiene hoy y aplica solo lo que
cambio.

LAS CLAVES NO VAN EN EL ARCHIVO. Hoy la clave SOL y la del certificado se tipean
una vez y el SFS las guarda cifradas: nunca tocan el disco en texto plano. Un .txt
con la clave SOL en la PC del cliente es otra cosa —se copia, se manda por chat,
queda en el Escritorio— asi que se piden cuando hacen falta, y solo cuando el
cambio las necesita.
"""
import os

# campo del archivo -> (parametro del SFS, etiqueta, a que endpoint pertenece)
#
# El endpoint importa porque decide que clave hay que pedir: 'emisor' necesita la
# clave SOL, 'certificado' la del certificado, y 'direccion' ninguna.
CAMPOS = (
    ("ruc",              "NUMRUC",   "RUC",              "emisor"),
    ("razon_social",     "RAZON",    "Razon social",     "emisor"),
    ("usuario_sol",      "USUSOL",   "Usuario SOL",      "emisor"),
    ("nombre_comercial", "NOMCOM",   "Nombre comercial", "direccion"),
    ("ubigeo",           "UBIGEO",   "Ubigeo",           "direccion"),
    ("direccion",        "DIRECC",   "Direccion",        "direccion"),
    ("departamento",     "DEPAR",    "Departamento",     "direccion"),
    ("provincia",        "PROVIN",   "Provincia",        "direccion"),
    ("distrito",         "DISTR",    "Distrito",         "direccion"),
    ("urbanizacion",     "URBANIZA", "Urbanizacion",     "direccion"),
)

OBLIGATORIOS = ("ruc", "razon_social", "usuario_sol", "ubigeo", "direccion",
                "departamento", "provincia", "distrito", "certificado", "base_datos")

PLANTILLA = """\
# Datos del contribuyente. Este archivo lo lee el instalador para configurar el
# SFS y para actualizarlo cuando algo cambia.
#
# LAS CLAVES NO VAN ACA: ni la clave SOL ni la del certificado. El instalador las
# pide cuando hacen falta y el SFS las guarda cifradas. Poner una clave en un
# archivo de texto es dejarla al alcance de cualquiera que use esa PC.

ruc              = {ruc}
razon_social     = {razon_social}
nombre_comercial = {nombre_comercial}
usuario_sol      = {usuario_sol}

# Direccion fiscal, tal como figura en la ficha RUC.
ubigeo           = {ubigeo}
direccion        = {direccion}
departamento     = {departamento}
provincia        = {provincia}
distrito         = {distrito}
urbanizacion     = {urbanizacion}

# Ruta al archivo .p12. Se copia a la carpeta CERT del SFS al aplicarlo.
certificado      = {certificado}

# Conexion a la base de la aplicacion, SIN la contrasena: el instalador la pide.
#   postgresql://usuario@host:5432/base?schema=public
#   sqlserver://usuario@host:1433/base
#   sqlserver://host:1433/base?trusted=yes
base_datos       = {base_datos}

ruta_sfs         = {ruta_sfs}
"""


def leer(ruta):
    """
    {campo: valor} del archivo. Lanza ValueError con el motivo si no se puede leer.

    Formato deliberadamente simple —clave = valor, '#' comenta— para que se pueda
    corregir con el Bloc de notas sin conocer ninguna sintaxis.
    """
    if not os.path.exists(ruta):
        raise ValueError(f"no existe el archivo {ruta}")
    datos = {}
    # utf-8-sig: tolera el BOM que deja el Bloc de notas.
    with open(ruta, encoding="utf-8-sig", errors="replace") as fh:
        for numero, linea in enumerate(fh, 1):
            l = linea.strip()
            if not l or l.startswith("#"):
                continue
            if "=" not in l:
                raise ValueError(f"linea {numero}: falta el '=' ({l[:40]})")
            clave, valor = l.split("=", 1)
            # Se corta el comentario al final de la linea, salvo que sea parte de
            # una ruta o una URL, donde '#' no separa nada.
            datos[clave.strip().lower()] = valor.strip()
    return datos


def escribir(ruta, datos):
    """Deja el archivo con la plantilla comentada y los valores que se le pasen."""
    completo = {c: "" for c in
                [n for n, _, _, _ in CAMPOS] + ["certificado", "base_datos", "ruta_sfs"]}
    completo.update({k: v for k, v in datos.items() if v is not None})
    with open(ruta, "w", encoding="utf-8") as fh:
        fh.write(PLANTILLA.format(**completo))
    return ruta


def validar(datos, validar_ruc=None):
    """Lista de problemas del archivo. Vacia si esta listo para aplicar."""
    problemas = []
    for campo in OBLIGATORIOS:
        if not datos.get(campo):
            problemas.append(f"falta {campo}")

    ruc = datos.get("ruc", "")
    if ruc and validar_ruc and not validar_ruc(ruc):
        problemas.append(f"el RUC {ruc} no es valido (digito verificador)")

    ubigeo = datos.get("ubigeo", "")
    if ubigeo and (not ubigeo.isdigit() or len(ubigeo) != 6):
        problemas.append(f"el ubigeo debe ser 6 digitos: {ubigeo}")

    cert = datos.get("certificado", "")
    if cert and not os.path.isfile(cert):
        problemas.append(f"no se encuentra el certificado: {cert}")
    elif cert and not cert.lower().endswith((".p12", ".pfx")):
        problemas.append(f"el certificado tiene que ser .p12 o .pfx: {cert}")

    url = datos.get("base_datos", "")
    if url and "://" not in url:
        problemas.append(f"base_datos tiene que ser una URL con '://': {url}")

    return problemas


def _certificado_cambio(ruta_origen, ruta_sfs, nombre_instalado):
    """
    True si el .p12 del archivo es distinto del que ya esta en el SFS.

    Se comparan los bytes y no el nombre: un certificado renovado suele llamarse
    igual que el que vence, asi que mirar el nombre diria "sin cambios" justo el
    dia que hay que reemplazarlo.
    """
    if not nombre_instalado:
        return True
    instalado = os.path.join(ruta_sfs, "sunat_archivos", "sfs", "CERT", nombre_instalado)
    if not os.path.isfile(instalado) or not os.path.isfile(ruta_origen):
        return True
    if os.path.getsize(instalado) != os.path.getsize(ruta_origen):
        return True
    with open(instalado, "rb") as a, open(ruta_origen, "rb") as b:
        return a.read() != b.read()


def comparar(datos, previo, ruta_sfs, url_actual=""):
    """
    Que cambia entre el archivo y lo instalado.

    Devuelve [(etiqueta, valor_actual, valor_nuevo, endpoint)] solo de lo que cambia.
    'previo' son los parametros que el SFS tiene hoy (chequeos.datos_sfs).
    """
    cambios = []
    for campo, parametro, etiqueta, endpoint in CAMPOS:
        nuevo = (datos.get(campo) or "").strip()
        actual = (previo.get(parametro) or "").strip()
        if nuevo and nuevo != actual:
            cambios.append((etiqueta, actual, nuevo, endpoint))

    cert = datos.get("certificado", "")
    if cert and _certificado_cambio(cert, ruta_sfs, previo.get("NOMCERT", "")):
        cambios.append(("Certificado", previo.get("NOMCERT", "") or "(ninguno)",
                        os.path.basename(cert), "certificado"))

    # La base no vive en el SFS sino en el .env, y la del archivo va sin clave: se
    # comparan sin credenciales para que no figure como cambio solo por eso.
    nueva_bd = _sin_credenciales(datos.get("base_datos", ""))
    if nueva_bd and nueva_bd != _sin_credenciales(url_actual):
        cambios.append(("Base de datos", _sin_credenciales(url_actual) or "(ninguna)",
                        nueva_bd, "base"))
    return cambios


def _sin_credenciales(url):
    """La URL sin usuario ni clave, para comparar solo el destino."""
    import urllib.parse
    if not url:
        return ""
    p = urllib.parse.urlsplit(url)
    destino = p.hostname or ""
    if p.port:
        destino += f":{p.port}"
    return f"{p.scheme}://{destino}{p.path}"


def desde_sfs(previo, ruta_sfs, url_actual=""):
    """Los datos actuales en formato de archivo, para poder exportarlos."""
    datos = {campo: previo.get(parametro, "") for campo, parametro, _, _ in CAMPOS}
    nombre = previo.get("NOMCERT", "")
    datos["certificado"] = (
        os.path.join(ruta_sfs, "sunat_archivos", "sfs", "CERT", nombre) if nombre else "")
    datos["base_datos"] = _sin_credenciales(url_actual)
    datos["ruta_sfs"] = ruta_sfs
    return datos
