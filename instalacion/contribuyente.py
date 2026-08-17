"""
Carga de los datos de un contribuyente en el SFS y en el daemon.

Las claves SOL y del certificado se le pasan al SFS para que las encripte EL,
igual que si se tipearan en su pantalla: replicar su algoritmo seria fragil y se
romperia con cualquier actualizacion suya.
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

import sistema

BETA = "https://e-beta.sunat.gob.pe/ol-ti-itcpfegem-beta/billService"
PRODUCCION = "https://e-factura.sunat.gob.pe/ol-ti-itcpfegem/billService"


# Datos del emisor que quedan en el SFS. Se vacian los valores pero NO se borran
# las filas: el SFS espera encontrarlas y las actualiza por COD_PARA cuando se le
# cargan los datos nuevos.
_CAMPOS_EMISOR = (
    "NUMRUC", "RAZON", "NOMCOM",
    "USUSOL", "CLASOL", "USUSOLPRINCIPAL", "CLASOLPRINCIPAL",
    "NOMCERT", "PRKCRT",
    "UBIGEO", "DIRECC", "DEPAR", "PROVIN", "DISTR", "URBANIZA",
    "CLIENT_ID", "CLIENT_SECRET",
)


def limpiar_contribuyente(ruta_sfs):
    """
    Borra del SFS los datos del contribuyente anterior y su certificado.

    Hace falta al reutilizar una PC para otro cliente: el SFS conserva el RUC, las
    credenciales SOL, el certificado y el historial de comprobantes, y emitir con
    eso seria facturar a nombre del contribuyente anterior.

    Devuelve (ok, mensaje). Respalda la base antes de tocarla.
    """
    import shutil
    import sqlite3

    bd = os.path.join(ruta_sfs, "bd", "BDFacturador.db")
    dir_cert = os.path.join(ruta_sfs, "sunat_archivos", "sfs", "CERT")

    borrados = 0
    if os.path.isdir(dir_cert):
        for archivo in os.listdir(dir_cert):
            if archivo.lower().endswith((".p12", ".pfx")):
                os.remove(os.path.join(dir_cert, archivo))
                borrados += 1

    if not os.path.exists(bd):
        return True, f"{borrados} certificado(s) eliminado(s); no habia base del SFS"

    # Si el operador se equivoco de PC, esto es lo unico que permite volver atras.
    respaldo = f"{bd}.bak-{datetime.now():%Y%m%d%H%M%S}"
    shutil.copy2(bd, respaldo)

    try:
        with sqlite3.connect(bd) as con:
            marcas = ",".join("?" * len(_CAMPOS_EMISOR))
            cur = con.execute(
                f"UPDATE PARAMETRO SET VAL_PARA='' WHERE COD_PARA IN ({marcas})",
                _CAMPOS_EMISOR,
            )
            parametros = cur.rowcount
            # El historial es del contribuyente anterior: si quedara, el daemon lo
            # leeria como documentos propios ya emitidos.
            cur = con.execute("DELETE FROM DOCUMENTO")
            documentos = cur.rowcount
    except sqlite3.Error as e:
        return False, f"no se pudo limpiar la base del SFS: {e}"

    return True, (f"{parametros} dato(s) del emisor vaciados, {documentos} comprobante(s) "
                  f"del historial y {borrados} certificado(s) eliminados\n"
                  f"         respaldo en {os.path.basename(respaldo)}")


def validar_ruc(ruc):
    """
    Digito verificador del RUC (modulo 11). Un RUC mal tipeado se descubriria
    recien cuando SUNAT rechaza el primer comprobante, y para entonces ya quedo
    todo configurado con el numero equivocado.
    """
    ruc = (ruc or "").strip()
    if len(ruc) != 11 or not ruc.isdigit():
        return False
    factores = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    suma = sum(int(d) * f for d, f in zip(ruc[:10], factores))
    dv = 11 - (suma % 11)
    if dv == 10:
        dv = 0
    if dv == 11:
        dv = 1
    return dv == int(ruc[10])


def revisar_certificado(ruta, clave, ruc):
    """
    (ok, mensaje, vence). Comprueba que se pueda abrir, que no este vencido y que
    su titular sea el RUC que se esta configurando: un .p12 de otro contribuyente
    falla en SUNAT con un error que no menciona al certificado.
    """
    try:
        from cryptography.hazmat.primitives.serialization import pkcs12
    except ImportError:
        return None, "no se pudo verificar (falta la libreria cryptography)", None

    try:
        with open(ruta, "rb") as fh:
            datos = fh.read()
        _, cert, _ = pkcs12.load_key_and_certificates(datos, clave.encode())
    except Exception:
        return False, "no se pudo abrir (contrasena incorrecta o archivo dañado)", None

    if cert is None:
        return False, "el archivo no contiene un certificado", None

    # not_valid_after_utc existe desde cryptography 42; antes era not_valid_after.
    vence = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
    vence = vence.replace(tzinfo=None)
    if vence < datetime.utcnow():
        return False, f"VENCIDO el {vence:%Y-%m-%d}", vence

    titular = cert.subject.rfc4514_string()
    if ruc and ruc not in titular:
        return False, f"no corresponde al RUC {ruc} (titular: {titular})", vence
    return True, f"vigente hasta {vence:%Y-%m-%d}", vence


def limpiar_url(texto):
    """
    Saca comillas y espacios de una URL pegada.

    En el .env de Prisma la URL va entrecomillada, asi que copiarla de ahi y pegarla
    trae las comillas puestas. Sin limpiarlas, urlsplit lee el esquema vacio y el
    error resultante —"no se reconoce el motor"— manda a buscar el problema al lugar
    equivocado.
    """
    limpio = (texto or "").strip()
    for comilla in ('"', "'", "“", "”"):
        if limpio.startswith(comilla):
            limpio = limpio[1:]
        if limpio.endswith(comilla):
            limpio = limpio[:-1]
    return limpio.strip()


def url_sin_clave(url):
    """La URL con la contrasena tapada, para poder mostrarla en pantalla."""
    import re
    return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", url or "") or "(sin configurar)"


def armar_url(motor, servidor, puerto, base, usuario="", clave="",
              esquema="public", windows=False, instancia=""):
    """
    La DATABASE_URL que va al .env, armada a partir de los datos sueltos.

    La clave se codifica: una contrasena con '@' o '/' partiria la URL en el lugar
    equivocado y el daemon terminaria conectando a otro servidor, o a ninguno.
    """
    credenciales = ""
    if usuario and not windows:
        credenciales = f"{urllib.parse.quote(usuario, safe='')}:" \
                       f"{urllib.parse.quote(clave, safe='')}@"
    if motor == "postgres":
        destino = f"{servidor}:{puerto}/{base}"
        return f"postgresql://{credenciales}{destino}?schema={esquema or 'public'}"

    # Con instancia con nombre no va el puerto: lo resuelve el SQL Browser, y ademas
    # suele ser dinamico.
    destino = f"{servidor}/{base}" if instancia else f"{servidor}:{puerto}/{base}"
    opciones = []
    if instancia:
        opciones.append(f"instancia={urllib.parse.quote(instancia, safe='')}")
    if windows:
        opciones.append("trusted=yes")
    return f"sqlserver://{credenciales}{destino}" + ("?" + "&".join(opciones) if opciones else "")


def necesita_clave(url):
    """
    True si la URL trae usuario pero no contrasena.

    Es el caso normal de la que viene en el archivo del cliente: se guarda sin clave
    a proposito. Con autenticacion de Windows no hay usuario y no falta nada.
    """
    p = urllib.parse.urlsplit(url or "")
    return bool(p.username) and not p.password


def con_clave(url, clave):
    """La misma URL con la contrasena puesta, codificada."""
    p = urllib.parse.urlsplit(url)
    if not p.username or p.password:
        return url
    credenciales = (f"{urllib.parse.quote(p.username, safe='')}:"
                    f"{urllib.parse.quote(clave, safe='')}")
    destino = p.hostname or ""
    if p.port:
        destino += f":{p.port}"
    return urllib.parse.urlunsplit(
        (p.scheme, f"{credenciales}@{destino}", p.path, p.query, p.fragment))


def usuario_de(url):
    """El usuario de la URL, para poder nombrarlo al pedir su clave."""
    return urllib.parse.unquote(urllib.parse.urlsplit(url or "").username or "")


def probar_base(url):
    """
    (ok, mensaje) conectando con el MISMO codigo que usa el daemon.

    Antes esto duplicaba la conexion de psycopg2, asi que solo sabia de PostgreSQL y
    podia quedar desincronizado del daemon sin que nadie lo notara. Ahora usa el
    adaptador que corresponda al motor: si el instalador dice que conecta, el daemon
    conecta.
    """
    try:
        import repositorio
    except ImportError:
        return False, "no se encontro el paquete repositorio/ del daemon"
    try:
        adaptador = repositorio.elegir(url)
    except RuntimeError as e:
        return False, str(e)
    except ImportError as e:
        # Falta el driver del motor elegido (psycopg2 o pyodbc).
        return False, f"falta el driver de ese motor: {e}"
    try:
        con = adaptador.conectar(url, 10)
        con.close()
        return True, f"conexion verificada ({repositorio.motor_de(url)})"
    except Exception as e:
        return False, str(e).strip().splitlines()[0]


def _post_sfs(base_url, ruta, cuerpo, timeout=60):
    peticion = urllib.request.Request(
        f"{base_url}/api/{ruta}",
        data=json.dumps(cuerpo).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"validacion": "FALLO", "mensaje": str(e)}


def _motivo(respuesta):
    """
    El texto de error de una respuesta del SFS.

    OJO: varios endpoints devuelven el motivo en la MISMA clave 'validacion' que
    usan para decir 'EXITO' —confirmado en el bytecode de importarCertificado, que
    hace put("validacion", "Debe ingresar la ruta del certificado")—. Buscarlo
    solo en 'mensaje' mostraba un 'None' inutil teniendo el motivo a la vista.
    """
    if not isinstance(respuesta, dict):
        return str(respuesta)
    # "FALLO" lo pone _post_sfs cuando ni siquiera se pudo llamar al SFS; en ese
    # caso el detalle esta en 'mensaje', no aca.
    validacion = respuesta.get("validacion")
    if validacion and validacion not in ("EXITO", "FALLO"):
        return str(validacion)
    for clave in ("mensaje", "Mensaje", "message", "error", "Error"):
        if respuesta.get(clave):
            return str(respuesta[clave])
    # Sin texto util: se muestra todo menos la bandeja, que son miles de caracteres.
    resumen = {k: v for k, v in respuesta.items() if k != "listaBandejaFacturador"}
    return f"el SFS no devolvio ningun detalle. Respuesta completa: {resumen}"


def esperar_sfs(base_url, segundos=90, avisar=print):
    """El SFS tarda en levantar; se espera a que conteste antes de configurarlo."""
    limite = time.time() + segundos
    while time.time() < limite:
        try:
            urllib.request.urlopen(f"{base_url}/", timeout=5).close()
            return True
        except Exception:
            time.sleep(2)
    return False


def cargar_emisor(base_url, datos, avisar=print):
    """RUC, razon social y credenciales SOL. Necesita la clave SOL."""
    # cmbFuncionamiento='02' y temporizadores vacios: el SFS NO debe correr sus
    # propios jobs de generar/enviar, porque harian por su cuenta lo mismo que el
    # daemon hace por REST y competirian por los mismos documentos.
    r = _post_sfs(base_url, "GrabarParametro.htm", {
        "txtNumeroRuc": datos["ruc"],
        "txtRazonSocial": datos["razon"],
        "txtUsuarioSol": datos["usuario_sol"],
        "txtClaveSol": datos["clave_sol"],
        "txtUsuarioSolPrincipal": datos["usuario_sol"],
        "txtClaveSolPrincipal": datos["clave_sol"],
        "txtRutaSolucion": datos["ruta_sfs"],
        "txtClient_id": "",
        "txtClient_secret": "",
        "cmbFuncionamiento": "02",
        "cmbTiempoGenera": "",
        "cmbTiempoEnvia": "",
    })
    if r.get("validacion") != "EXITO":
        return False, f"el SFS rechazo los datos del emisor: {_motivo(r)}"
    avisar("    emisor y credenciales SOL cargados (el SFS encripta las claves)")
    return True, ""


def cargar_direccion(base_url, datos, avisar=print):
    """Nombre comercial y direccion fiscal. No lleva ninguna clave."""
    r = _post_sfs(base_url, "GrabarOtrosParametros.htm", {
        "txtNombreComercial": datos["comercial"],
        "txtUbigeo": datos["ubigeo"],
        "txtDireccion": datos["direccion"],
        "txtDepartamento": datos["departamento"],
        "txtProvincia": datos["provincia"],
        "txtDistrito": datos["distrito"],
        "txtUrbanizacion": datos.get("urbanizacion", ""),
    })
    if r.get("validacion") != "EXITO":
        return False, f"el SFS rechazo la direccion: {_motivo(r)}"
    avisar("    direccion fiscal cargada")
    return True, ""


def cargar_certificado(base_url, datos, avisar=print):
    """Importa el .p12. Necesita la contrasena del certificado."""
    # El SFS NO usa la ruta que se le pasa: arma la suya pegando su carpeta CERT
    # con lo que reciba en 'nombreCertificado' (verificado en el bytecode de
    # importarCertificado). Mandarle una ruta completa produce algo como
    # "...CERT + C:/otra/ruta/cert.p12", que no existe, y el SFS solo responde
    # "el certificado no fue creado" sin decir que el problema es la ruta.
    # Asi que el archivo se copia a CERT y se le manda unicamente el nombre.
    nombre = _copiar_a_cert(datos["ruta_sfs"], datos["ruta_certificado"])
    r = _post_sfs(base_url, "ImportarCertificado.htm", {
        "nombreCertificado": nombre,
        "passPrivateKey": datos["clave_certificado"],
    })
    if r.get("validacion") != "EXITO":
        return False, f"el SFS rechazo el certificado: {_motivo(r)}"
    avisar(f"    certificado importado ({nombre})")
    return True, ""


def cargar_en_sfs(base_url, datos, avisar=print):
    """
    Los tres endpoints, en orden. Devuelve (ok, mensaje_error).

    Se corta en el primero que falle para no dejar una configuracion a medias. Son
    independientes entre si, y por eso una reconfiguracion puede llamar solo al que
    corresponde en vez de rehacer todo (ver perfil.py).
    """
    for cargar in (cargar_emisor, cargar_direccion, cargar_certificado):
        listo, error = cargar(base_url, datos, avisar)
        if not listo:
            return False, error
    return True, ""


def _copiar_a_cert(ruta_sfs, origen):
    """Deja el certificado en la carpeta CERT del SFS y devuelve su nombre."""
    import shutil
    destino_dir = os.path.join(ruta_sfs, "sunat_archivos", "sfs", "CERT")
    os.makedirs(destino_dir, exist_ok=True)
    nombre = os.path.basename(origen)
    destino = os.path.join(destino_dir, nombre)
    # Si ya es el mismo archivo no hay nada que copiar: copiarlo sobre si mismo
    # falla y ademas lo dejaria en cero.
    if os.path.abspath(origen) != os.path.abspath(destino):
        shutil.copy2(origen, destino)
    return nombre


def fijar_ambiente(ruta_sfs, produccion):
    """
    Deja activa una sola linea RUTA_SERV_CDP. El SFS toma la primera sin comentar,
    asi que dos activas serian ambiguas.
    """
    archivo = os.path.join(ruta_sfs, "sunat_archivos", "sfs", "VALI", "constantes.properties")
    if not os.path.exists(archivo):
        return False, f"no se encontro {archivo}"
    destino = PRODUCCION if produccion else BETA

    # utf-8-sig por si el archivo trae un BOM de haber sido editado con el Bloc de notas.
    with open(archivo, encoding="utf-8-sig", errors="replace") as fh:
        lineas = fh.read().splitlines()

    salida, encontrada = [], False
    for linea in lineas:
        pelada = linea.strip()
        if pelada.startswith("RUTA_SERV_CDP=") or pelada.startswith("#RUTA_SERV_CDP="):
            url = pelada.lstrip("#").split("=", 1)[1].strip()
            if url == destino:
                salida.append(f"RUTA_SERV_CDP={destino}")
                encontrada = True
            else:
                salida.append("#" + pelada.lstrip("#"))
        else:
            salida.append(linea)
    if not encontrada:
        salida.append(f"RUTA_SERV_CDP={destino}")

    # Sin BOM: ese caracter invisible al inicio corrompe el nombre de la primera
    # propiedad para quien lea el archivo.
    with open(archivo, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(salida) + "\n")
    return True, destino


def escribir_config_pm2(raiz, ruta_sfs):
    """
    Genera sfs.config.js con las rutas reales de esta instalacion.

    El archivo versionado trae rutas absolutas fijas, que solo sirven en la PC
    donde se escribio: si el proyecto o el SFS quedan en otra carpeta, PM2
    intenta arrancar los procesos en directorios que no existen. Y como lee todo
    el archivo aunque se le pida un solo proceso, alcanza con que una ruta este
    mal para que falle.
    """
    contenido = """// Generado por el instalador: las rutas son las de ESTA instalacion.
module.exports = {
  apps: [
    {
      name: "sfs",
      script: "java",
      // Sin -Xmx, el JVM se reserva hasta 1/4 de la RAM del equipo y usa el
      // recolector paralelo (un hilo por nucleo). Para una app que procesa unos
      // pocos documentos por dia eso es desperdicio, y en una PC de 8 GB deja al
      // SFS sin memoria para arrancar. Los topes no reservan nada: solo impiden
      // que crezca sin control.
      args: "-Xms64m -Xmx512m -XX:MaxMetaspaceSize=256m -XX:+UseSerialGC "
            + "-jar facturadorApp-%(version)s.jar server prod.yaml",
      cwd: %(sfs)s,
      watch: false,
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 10,
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      error_file: "logs/sfs-error.log",
      out_file: "logs/sfs-out.log",
      merge_logs: true,
    },
    {
      name: "facturador",
      script: "main.py",
      interpreter: "python",
      cwd: %(raiz)s,
      watch: false,
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 10,
      env: {
        PYTHONUNBUFFERED: "1",
        PYTHONIOENCODING: "utf-8",
      },
      log_date_format: "YYYY-MM-DD HH:mm:ss",
      error_file: "logs/error.log",
      out_file: "logs/out.log",
      merge_logs: true,
    },
  ],
};
""" % {
        # json.dumps escapa las barras invertidas de las rutas de Windows.
        "sfs": json.dumps(ruta_sfs),
        "raiz": json.dumps(raiz),
        "version": sistema.VERSION_SFS,
    }
    destino = os.path.join(raiz, "sfs.config.js")
    with open(destino, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(contenido)
    return destino


def actualizar_env(raiz, cambios):
    """
    Cambia solo las claves indicadas del .env, dejando el resto tal cual.

    Hace falta porque escribir_env() rehace el archivo entero: usarlo para cambiar
    un solo dato borraria SOL_CLAVE —que no se puede recuperar de ningun lado— sin
    que nadie lo note hasta que ningun resumen diario vuelva a cerrarse.

    Devuelve (ruta_respaldo, claves_agregadas).
    """
    destino = os.path.join(raiz, ".env")
    if not os.path.exists(destino):
        return None, []

    with open(destino, encoding="utf-8-sig", errors="replace") as fh:
        lineas = fh.read().splitlines()

    pendientes = dict(cambios)
    salida = []
    for linea in lineas:
        limpia = linea.strip()
        if limpia and not limpia.startswith("#") and "=" in limpia:
            clave = limpia.split("=", 1)[0].strip()
            if clave in pendientes:
                salida.append(f"{clave}={pendientes.pop(clave)}")
                continue
        salida.append(linea)

    # Lo que no estaba en el archivo se agrega al final, para no perderlo.
    agregadas = list(pendientes)
    for clave, valor in pendientes.items():
        salida.append(f"{clave}={valor}")

    respaldo = f"{destino}.bak-{datetime.now():%Y%m%d%H%M%S}"
    os.replace(destino, respaldo)
    with open(destino, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(salida) + "\n")
    _restringir_permisos(destino)
    return respaldo, agregadas


def escribir_env(raiz, datos, ruta_sfs):
    """Genera el .env del daemon. Devuelve la ruta del respaldo, si hubo."""
    contenido = f"""# Generado por el instalador el {datetime.now():%Y-%m-%d %H:%M}
DATABASE_URL={datos['database_url']}

SFS_BASE_URL=http://localhost:9000
SFS_DATA_DIR={ruta_sfs}\\sunat_archivos\\sfs\\DATA
SFS_RPTA_DIR={ruta_sfs}\\sunat_archivos\\sfs\\RPTA
SFS_BD_PATH={ruta_sfs}\\bd\\BDFacturador.db

EMISOR_RUC={datos['ruc']}

INTERVALO_GENERACION_SEG=60
MAX_REINTENTOS_RECHAZO=3

# Necesarias para cerrar los resumenes diarios: su CDR llega detras de un ticket.
SOL_USUARIO={datos['usuario_sol']}
SOL_CLAVE={datos['clave_sol']}
CONSULTA_SUNAT_TRAS_MIN=10

# El desfase con el reloj de la BD se mide solo en cada ciclo.
DESFASE_BD_HORAS=auto
"""
    destino = os.path.join(raiz, ".env")
    respaldo = None
    if os.path.exists(destino):
        respaldo = f"{destino}.bak-{datetime.now():%Y%m%d%H%M%S}"
        os.replace(destino, respaldo)
    # Sin BOM: con el, python-dotenv leeria la primera variable como
    # "﻿DATABASE_URL" y el daemon diria que falta teniendola.
    with open(destino, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(contenido)
    restringido = _restringir_permisos(destino)
    return respaldo, restringido


def _restringir_permisos(archivo):
    """
    Limita el .env a su dueño: lleva la clave SOL y la de la base en texto plano.

    Se comprueba despues que siga siendo legible, y si no se revierte. Sin esa
    verificacion el endurecimiento puede dejar el archivo sin acceso para nadie
    —paso: quitar la herencia y que el permiso otorgado no se aplique— y entonces
    el daemon no arranca, con un PermissionError que no sugiere de donde viene.
    """
    usuario = os.environ.get("USERNAME", "")
    if not usuario:
        return
    dominio = os.environ.get("USERDOMAIN", "")
    cuenta = f"{dominio}\\{usuario}" if dominio else usuario
    sistema.correr(f'icacls "{archivo}" /inheritance:r /grant:r "{cuenta}:(F)"', timeout=30)

    try:
        with open(archivo, encoding="utf-8") as fh:
            fh.read(1)
    except OSError:
        # Mejor un archivo legible de mas que una instalacion que no arranca.
        sistema.correr(f'icacls "{archivo}" /reset', timeout=30)
        return False
    return True
