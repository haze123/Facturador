"""
Instalador del Facturador SUNAT.

Se compila a un unico .exe con PyInstaller (ver construir.py), asi que corre en
una PC donde todavia no hay Python: el interprete viaja adentro del ejecutable.

Del 1 al 4, en el orden en que se hacen al instalar por primera vez:
  1. Instalar el entorno   (prerrequisitos, PM2, descarga del SFS)
  2. Configurar un cliente (datos del contribuyente, .env, procesos)
  3. Verificar             (diagnostico de una instalacion existente)
  4. Pasar a produccion    (cambia el ambiente del SFS y limpia las pruebas)

Y aparte, para cuando la instalacion ya existe:
  5. Actualizar desde el archivo del cliente (aplica solo lo que cambio)
"""
import os
import sys

import contribuyente
import sistema

# Al correr como .exe, sys.executable apunta al propio ejecutable; el proyecto
# esta en la carpeta que lo contiene o en su padre.
if getattr(sys, "frozen", False):
    _AQUI = os.path.dirname(sys.executable)
else:
    _AQUI = os.path.dirname(os.path.abspath(__file__))
RAIZ = _AQUI if os.path.exists(os.path.join(_AQUI, "main.py")) else os.path.dirname(_AQUI)


# --- salida ----------------------------------------------------------------

class C:
    """Colores ANSI. La consola de Windows 10+ los entiende."""
    VERDE = "\033[92m"; ROJO = "\033[91m"; AMAR = "\033[93m"
    CYAN = "\033[96m"; GRIS = "\033[90m"; NEGRITA = "\033[1m"; FIN = "\033[0m"


def titulo(t):
    print(f"\n{C.CYAN}{t}{C.FIN}\n{C.GRIS}{'-' * len(t)}{C.FIN}")


def ok(t):    print(f"  {C.VERDE}[ OK ]{C.FIN} {t}")
def error(t): print(f"  {C.ROJO}[ERROR]{C.FIN} {t}")
def aviso(t): print(f"  {C.AMAR}[AVISO]{C.FIN} {t}")
def nota(t, fin="\n"): print(f"{C.GRIS}{t}{C.FIN}", end=fin, flush=True)
def paso(t):  print(f"  {t}")


def preguntar(etiqueta, actual="", obligatorio=False):
    """Pregunta mostrando el valor actual entre corchetes; Enter lo conserva."""
    while True:
        sufijo = f" [{actual}]" if actual else ""
        r = input(f"  {etiqueta}{sufijo}: ").strip()
        if not r and actual:
            return actual
        if r:
            return r
        if not obligatorio:
            return ""
        print(f"    {C.AMAR}(este dato es obligatorio){C.FIN}")


def preguntar_clave(etiqueta):
    import getpass
    return getpass.getpass(f"  {etiqueta}: ")


def confirmar(pregunta):
    return input(f"  {pregunta} (si/no): ").strip().lower() in ("si", "s", "sí")


def elegir_opcion(etiqueta, opciones, por_defecto="1"):
    """Menu corto de una sola tecla. Devuelve la clave elegida."""
    for clave, texto in opciones:
        print(f"    {clave}  {texto}")
    while True:
        r = input(f"  {etiqueta} [{por_defecto}]: ").strip() or por_defecto
        if r in dict(opciones):
            return r
        print(f"    {C.AMAR}(elegir una de las opciones){C.FIN}")


def pedir_base_de_datos():
    """
    Los datos de conexion segun el motor. Devuelve la DATABASE_URL, o "" si se cancela.

    Se pregunta por partes porque nadie recuerda la sintaxis de la URL de memoria, y
    una mal escrita falla con un mensaje del driver que no dice cual es el pedazo
    equivocado.

    Pegarla entera se ofrece SOLO para PostgreSQL, y a proposito: la URL que el
    cliente ya tiene armada es la de Prisma, que es de PostgreSQL. Un cliente de SQL
    Server no tiene ninguna con este formato —la suya seria una cadena ODBC o de
    .NET, que no sirve acá—, asi que ofrecersela seria ofrecerle algo que no existe.
    """
    motor = elegir_opcion("Motor", [
        ("1", "PostgreSQL"),
        ("2", "SQL Server"),
    ])

    pegar = motor == "1" and elegir_opcion("Como cargar la conexion", [
        ("1", "Pegar la DATABASE_URL completa (la que ya usa la aplicacion)"),
        ("2", "Escribir servidor, puerto, base, usuario y clave por separado"),
    ]) == "1"

    while True:
        if pegar:
            url = preguntar("DATABASE_URL", "", obligatorio=True)
        elif motor == "1":
            servidor = preguntar("Servidor (host o IP)", "localhost", obligatorio=True)
            puerto   = preguntar("Puerto", "5432")
            base     = preguntar("Base de datos", "postgres", obligatorio=True)
            usuario  = preguntar("Usuario", "", obligatorio=True)
            clave    = preguntar_clave("Clave (no se ve al escribir)")
            esquema  = preguntar("Schema", "public")
            url = contribuyente.armar_url("postgres", servidor, puerto, base,
                                          usuario, clave, esquema=esquema)
        else:
            servidor = preguntar("Servidor (host o IP)", "localhost", obligatorio=True)
            puerto   = preguntar("Puerto", "1433")
            base     = preguntar("Base de datos", "", obligatorio=True)
            # Con autenticacion de Windows el daemon entra con el usuario que corre
            # el proceso; sirve cuando el SQL Server esta en la misma PC.
            windows  = confirmar("Usar autenticacion de Windows?")
            usuario = clave = ""
            if not windows:
                usuario = preguntar("Usuario de SQL Server", "sa", obligatorio=True)
                clave   = preguntar_clave("Clave (no se ve al escribir)")
            url = contribuyente.armar_url("sqlserver", servidor, puerto, base,
                                          usuario, clave, windows=windows)

        nota(f"         {contribuyente.url_sin_clave(url)}")
        listo, mensaje = contribuyente.probar_base(url)
        if listo:
            ok(mensaje)
            return url
        error(f"no se pudo conectar: {mensaje}")
        if not confirmar("Volver a intentar?"):
            return ""


def pausa():
    input(f"\n  {C.GRIS}Enter para volver al menu...{C.FIN}")


# --- 1. Instalar el entorno -------------------------------------------------

def instalar_entorno():
    print(f"\n{C.NEGRITA}  INSTALAR EL ENTORNO{C.FIN}")
    nota("  Deja la PC lista, sin datos de ningun contribuyente.")

    titulo("1. Prerrequisitos")
    faltan = []
    for nombre, presente, detalle, req in sistema.revisar_prerrequisitos():
        if presente:
            ok(detalle or nombre)
            continue
        aviso(f"{nombre}: falta")
        instalado, mensaje = sistema.instalar_con_winget(req, avisar=paso)
        if instalado:
            ok(f"{nombre} instalado")
        else:
            error(mensaje or f"no se pudo instalar {nombre}")
            faltan.append(f"{nombre} -> {req['manual']}")

    if faltan:
        print()
        for f in faltan:
            error(f)
        nota("\n  Si se acaban de instalar, cerrar este programa y volver a abrirlo.")
        return False

    titulo("2. Dependencias de Python")
    requisitos = os.path.join(RAIZ, "requirements.txt")
    if not os.path.exists(requisitos):
        error(f"no se encontro {requisitos}")
        return False
    paso("instalando psycopg2, python-dotenv, watchdog...")
    listo, salida, sin_instalar = sistema.instalar_dependencias_python(requisitos)
    if listo:
        ok("psycopg2, dotenv, watchdog")
    else:
        error(f"no se pudieron instalar: {', '.join(sin_instalar)}")
        nota("  " + "\n  ".join(salida.strip().splitlines()[-3:]))
        return False

    titulo("3. PM2")
    for nombre, presente in sistema.instalar_pm2():
        (ok if presente else aviso)(nombre + ("" if presente else ": no se pudo instalar"))

    titulo(f"4. Facturador SUNAT (SFS {sistema.VERSION_SFS})")
    ruta = preguntar("Instalar el SFS en", sistema.RUTA_SFS_POR_DEFECTO)
    listo, mensaje = sistema.descargar_sfs(ruta, avisar=nota)
    if not listo:
        error(mensaje)
        return False
    ok(mensaje)

    print(f"\n{C.VERDE}  ENTORNO LISTO{C.FIN}")
    nota("  Ahora corresponde la opcion 2: configurar el cliente.")
    return True


# --- 2. Configurar un cliente ----------------------------------------------

def configurar_cliente():
    print(f"\n{C.NEGRITA}  CONFIGURAR UN CLIENTE{C.FIN}")
    nota("  Queda apuntando a BETA. Pasar a produccion es un paso aparte,")
    nota("  despues de emitir una prueba y verla aceptada.")

    ruta_sfs = preguntar("Ruta del SFS", sistema.RUTA_SFS_POR_DEFECTO)
    if not os.path.exists(os.path.join(ruta_sfs, f"facturadorApp-{sistema.VERSION_SFS}.jar")):
        error(f"no se encontro el SFS en {ruta_sfs}. Correr primero la opcion 1.")
        return False

    # Si ya hay configuracion, se ofrece como valor por defecto.
    import chequeos
    previo = chequeos.datos_sfs(os.path.join(ruta_sfs, "bd", "BDFacturador.db"))

    # Reutilizar una PC que ya tenia otro cliente no puede pasar inadvertido:
    # emitir con el certificado de otro contribuyente es facturar a su nombre.
    if previo.get("NUMRUC"):
        titulo("Esta PC ya tiene un contribuyente configurado")
        nota(f"  RUC: {previo['NUMRUC']}   {previo.get('RAZON', '')}")
        print()
        aviso("Si es para OTRO cliente hay que borrar sus datos y su certificado.")
        if confirmar("Borrar los datos del contribuyente anterior?"):
            listo, mensaje = contribuyente.limpiar_contribuyente(ruta_sfs)
            if not listo:
                error(mensaje)
                return False
            ok(mensaje)
            previo = {}
        else:
            nota("  Se conservan; verificar que sean del cliente correcto.")

    titulo("1. Datos del contribuyente")
    while True:
        ruc = preguntar("RUC (11 digitos)", previo.get("NUMRUC", ""), obligatorio=True)
        if contribuyente.validar_ruc(ruc):
            break
        error(f"el RUC '{ruc}' no es valido (no pasa el digito verificador)")

    razon = preguntar("Razon social", previo.get("RAZON", ""), obligatorio=True)
    comercial = preguntar("Nombre comercial", previo.get("NOMCOM", "") or razon)
    usuario_sol = preguntar("Usuario SOL secundario", previo.get("USUSOL", ""), obligatorio=True)
    clave_sol = preguntar_clave("Clave SOL (no se ve al escribir)")
    if not clave_sol:
        error("la clave SOL es obligatoria")
        return False

    titulo("2. Direccion fiscal")
    nota("  Cada parte va por separado: el SFS las combina al armar el XML.")
    ubigeo = preguntar("Ubigeo (6 digitos)", previo.get("UBIGEO", ""), obligatorio=True)
    nota("  Solo calle y numero. Ejemplo: AV. IZAGUIRRE NRO. 785 DPTO. 302")
    direccion = preguntar("Direccion (sin distrito ni urbanizacion)", "", obligatorio=True)
    departamento = preguntar("Departamento", "LIMA", obligatorio=True)
    provincia = preguntar("Provincia", "LIMA", obligatorio=True)
    distrito = preguntar("Distrito", "", obligatorio=True)
    urbanizacion = preguntar("Urbanizacion (opcional). Ej: URB. MERCURIO ETAPA 1")

    titulo("3. Certificado digital")
    nota("  El archivo .p12 o .pfx que emitio la entidad certificadora para este RUC.")
    nota(r"  Ejemplo: C:\certificado.p12")
    while True:
        ruta_cert = preguntar("Ruta del ARCHIVO .p12", "", obligatorio=True).strip('"')
        if os.path.isfile(ruta_cert):
            if ruta_cert.lower().endswith((".p12", ".pfx")):
                break
            error("el archivo tiene que ser .p12 o .pfx")
        elif os.path.isdir(ruta_cert):
            # Sin esto se aceptaba la carpeta y fallaba despues culpando a la
            # contrasena, que es lo ultimo donde uno buscaria el problema.
            error("eso es una carpeta; hace falta la ruta del archivo .p12 que esta adentro")
            certs = [f for f in os.listdir(ruta_cert) if f.lower().endswith((".p12", ".pfx"))]
            if certs:
                nota("  En esa carpeta hay: " + ", ".join(certs))
                nota(f"  Probar con: {os.path.join(ruta_cert, certs[0])}")
            else:
                nota("  Esa carpeta no tiene ningun .p12: hay que copiar ahi el del cliente.")
        else:
            error(f"no existe '{ruta_cert}'")
    clave_cert = preguntar_clave("Contrasena del certificado (no se ve al escribir)")

    # Se valida ANTES de tocar el SFS: un certificado de otro contribuyente falla
    # en SUNAT con un error que no menciona al certificado.
    valido, mensaje, _ = contribuyente.revisar_certificado(ruta_cert, clave_cert, ruc)
    if valido is False:
        error(f"certificado: {mensaje}")
        return False
    (ok if valido else aviso)(f"certificado: {mensaje}")

    titulo("4. Base de datos de la aplicacion")
    db_url = pedir_base_de_datos()
    if not db_url:
        return False

    datos = {
        "ruc": ruc, "razon": razon, "comercial": comercial,
        "usuario_sol": usuario_sol, "clave_sol": clave_sol,
        "ubigeo": ubigeo, "direccion": direccion, "departamento": departamento,
        "provincia": provincia, "distrito": distrito, "urbanizacion": urbanizacion,
        "ruta_certificado": ruta_cert, "clave_certificado": clave_cert,
        "database_url": db_url, "ruta_sfs": ruta_sfs,
    }

    titulo("5. Configurando el SFS")
    # Antes de arrancar nada: el sfs.config.js versionado trae rutas fijas de otra
    # PC, y PM2 lee el archivo entero aunque se le pida un solo proceso.
    config = contribuyente.escribir_config_pm2(RAIZ, ruta_sfs)
    ok(f"{os.path.basename(config)} generado con las rutas de esta PC")

    base_url = "http://localhost:9000"
    if not contribuyente.esperar_sfs(base_url, segundos=5):
        paso("levantando el SFS (puede tardar hasta un minuto)...")
        codigo, salida = sistema.correr(["pm2", "start", config, "--only", "sfs"], timeout=120)
        if not contribuyente.esperar_sfs(base_url, segundos=90):
            error("el SFS no respondio despues de 90 segundos")
            # Sin esto habria que ir a buscar el motivo a los logs de PM2.
            nota("  Respuesta de PM2:")
            for linea in salida.strip().splitlines()[-6:]:
                nota(f"    {linea}")
            codigo, log = sistema.correr("pm2 logs sfs --lines 12 --nostream", timeout=60)
            if log.strip():
                nota("  Ultimas lineas del SFS:")
                for linea in log.strip().splitlines()[-8:]:
                    nota(f"    {linea}")
            return False
    ok("el SFS responde")

    listo, mensaje = contribuyente.cargar_en_sfs(base_url, datos, avisar=paso)
    if not listo:
        error(mensaje)
        return False

    titulo("6. Ambiente de SUNAT")
    listo, destino = contribuyente.fijar_ambiente(ruta_sfs, produccion=False)
    if not listo:
        error(destino)
        return False
    ok("BETA (los comprobantes NO tienen validez fiscal)")
    nota(f"  {destino}")

    titulo("7. Daemon")
    respaldo, restringido = contribuyente.escribir_env(RAIZ, datos, ruta_sfs)
    if respaldo:
        nota(f"  se respaldo el .env anterior en {os.path.basename(respaldo)}")
    if restringido:
        ok(".env escrito y restringido al usuario actual")
    else:
        ok(".env escrito")
        aviso("no se pudo restringir su acceso; lleva claves en texto plano")

    # El archivo del cliente se genera aca y no despues: en este punto estan todos
    # los datos recien tipeados. Sin esto habria que volver a juntarlos a mano la
    # primera vez que haga falta actualizar algo (opcion 5), o al reinstalar tras un
    # formateo. No lleva ninguna clave.
    import perfil
    ruta_perfil = perfil.escribir(os.path.join(RAIZ, "cliente.conf"), {
        "ruc": datos["ruc"], "razon_social": datos["razon"],
        "nombre_comercial": datos["comercial"], "usuario_sol": datos["usuario_sol"],
        "ubigeo": datos["ubigeo"], "direccion": datos["direccion"],
        "departamento": datos["departamento"], "provincia": datos["provincia"],
        "distrito": datos["distrito"], "urbanizacion": datos.get("urbanizacion", ""),
        "certificado": datos["ruta_certificado"],
        "base_datos": perfil.url_para_archivo(datos["database_url"]),
        "ruta_sfs": ruta_sfs,
    })
    ok(f"datos del cliente guardados en {os.path.basename(ruta_perfil)}")
    nota("         Sirve para actualizar despues (opcion 5) sin volver a cargar todo.")
    nota("         No lleva claves: conviene guardar una copia fuera de esta PC.")

    sistema.correr(["pm2", "start", config])
    sistema.correr(["pm2", "save"])
    codigo, _ = sistema.correr(["pm2-startup", "install"], timeout=120)
    ok("SFS y daemon registrados en PM2")
    if sistema.hay("pm2-startup"):
        ok("arranque automatico con Windows")
    else:
        aviso("sin arranque automatico: al prender la PC hay que correr 'pm2 resurrect'")

    print(f"\n{C.VERDE}  LISTO - la instalacion quedo en BETA{C.FIN}")
    nota("\n  Antes de pasar a produccion:")
    nota("   1. Emitir un comprobante de prueba y confirmar que SUNAT lo acepte")
    nota("   2. Borrar los comprobantes de prueba de la base")
    nota("   3. Volver a este menu y elegir 'Pasar a produccion'")
    return True


def pasar_a_produccion():
    print(f"\n{C.NEGRITA}  PASAR A PRODUCCION{C.FIN}")
    print(f"\n  {C.ROJO}Desde este momento los comprobantes tendran validez fiscal.{C.FIN}")
    nota("  Hacerlo solo despues de haber emitido una prueba en beta y verla aceptada.")
    if input("\n  Escribir PRODUCCION para continuar: ").strip() != "PRODUCCION":
        aviso("cancelado")
        return False

    ruta_sfs = preguntar("Ruta del SFS", sistema.RUTA_SFS_POR_DEFECTO)
    listo, destino = contribuyente.fijar_ambiente(ruta_sfs, produccion=True)
    if not listo:
        error(destino)
        return False
    ok("apuntando a PRODUCCION")
    nota(f"  {destino}")
    paso("reiniciando el SFS para que tome el cambio...")
    sistema.correr(["pm2", "restart", "sfs"])
    ok("listo")
    return True


# --- Menu -------------------------------------------------------------------

def actualizar_desde_archivo():
    """
    Lee el archivo del cliente, muestra que cambia y aplica solo eso.

    Evita el camino de "reconfigurar todo" cuando lo unico que paso es que vencio el
    certificado o cambiaron un dato: cada endpoint del SFS es independiente, asi que
    se llama unicamente al que corresponde, y se piden solo las claves que ese
    endpoint necesita.
    """
    import chequeos
    import perfil

    ruta_sfs = preguntar("Ruta del SFS", sistema.RUTA_SFS_POR_DEFECTO)
    ruta_bd = os.path.join(ruta_sfs, "bd", "BDFacturador.db")
    previo = chequeos.datos_sfs(ruta_bd)
    if not previo.get("NUMRUC"):
        error("esta PC todavia no tiene un contribuyente configurado")
        nota("         Usar primero la opcion 2.")
        return False

    ruta_archivo = preguntar("Archivo del cliente", os.path.join(RAIZ, "cliente.conf"))
    if not os.path.exists(ruta_archivo):
        aviso(f"no existe {ruta_archivo}")
        if not confirmar("Crearlo con los datos que ya tiene esta PC?"):
            return False
        env = chequeos._leer_env(RAIZ) or {}
        perfil.escribir(ruta_archivo,
                        perfil.desde_sfs(previo, ruta_sfs, env.get("DATABASE_URL", "")))
        ok(f"creado {ruta_archivo}")
        nota("         Editarlo con el Bloc de notas y volver a esta opcion.")
        return True

    try:
        datos = perfil.leer(ruta_archivo)
    except ValueError as e:
        error(f"no se pudo leer el archivo: {e}")
        return False

    problemas = perfil.validar(datos, contribuyente.validar_ruc)
    if problemas:
        error("el archivo tiene problemas:")
        for p in problemas:
            nota(f"         - {p}")
        return False
    ok("archivo valido")

    # El archivo tiene que ser de ESTE contribuyente. Teniendo uno por cliente, abrir
    # el equivocado es cuestion de tiempo, y aplicarlo reconfiguraria la PC con los
    # datos de otro sin limpiar nada: se notaria recien cuando SUNAT rechazara los
    # comprobantes por no coincidir el certificado.
    ajeno = perfil.verificar_identidad(datos, previo)
    if ajeno:
        error(ajeno)
        nota("         Un RUC distinto es otro contribuyente. Si esta PC pasa a otro")
        nota("         cliente, corresponde la opcion 2: ademas de cargar los datos")
        nota("         nuevos, limpia el historial y el certificado del anterior.")
        return False
    ok(f"el archivo es del contribuyente instalado ({datos['ruc']})")

    env = chequeos._leer_env(RAIZ) or {}
    cambios = perfil.comparar(datos, previo, ruta_sfs, env.get("DATABASE_URL", ""))
    if cambios:
        titulo("Cambios a aplicar")
        for etiqueta, actual, nuevo, _ in cambios:
            print(f"    {etiqueta:20} {actual or '(vacio)'}")
            print(f"    {'':20} {C.CYAN}-> {nuevo}{C.FIN}")

    else:
        ok("el archivo coincide con lo instalado: ningun campo cambio")

    # Las claves se preguntan aparte y siempre, porque son lo unico que la
    # comparacion NO puede detectar: no estan en el archivo y el SFS las guarda
    # cifradas, asi que no hay contra que compararlas. Sin este paso, un cliente que
    # solo cambio su clave SOL no tendria por donde actualizarla.
    titulo("Claves")
    nota("         No se pueden comparar: el SFS las guarda cifradas y el archivo no")
    nota("         las lleva. Si alguna cambio, hay que decirlo aca.")
    eleccion = elegir_opcion("Actualizar alguna clave", [
        ("1", "Ninguna"),
        ("2", "La clave SOL"),
        ("3", "La contrasena del certificado"),
        ("4", "Las dos"),
    ])

    afectados = {endpoint for _, _, _, endpoint in cambios}
    if eleccion in ("2", "4"):
        afectados.add("emisor")
    if eleccion in ("3", "4"):
        afectados.add("certificado")

    if not afectados:
        ok("no hay nada que aplicar")
        return True

    print()
    if not confirmar("Aplicar?"):
        return False

    base_url = "http://localhost:9000"
    datos_sfs = {
        "ruc": datos["ruc"], "razon": datos["razon_social"],
        "comercial": datos.get("nombre_comercial") or datos["razon_social"],
        "usuario_sol": datos["usuario_sol"], "ubigeo": datos["ubigeo"],
        "direccion": datos["direccion"], "departamento": datos["departamento"],
        "provincia": datos["provincia"], "distrito": datos["distrito"],
        "urbanizacion": datos.get("urbanizacion", ""),
        "ruta_sfs": datos.get("ruta_sfs") or ruta_sfs,
        "ruta_certificado": datos["certificado"],
    }

    # Cada clave se pide solo si su endpoint esta entre los afectados: porque algun
    # campo suyo cambio, o porque se pidio actualizar esa clave.
    if "emisor" in afectados:
        titulo("Clave SOL")
        nota("         El endpoint del emisor la lleva siempre, y el SFS no la devuelve")
        nota("         nunca: aunque no haya cambiado, hay que volver a escribirla.")
        datos_sfs["clave_sol"] = preguntar_clave("Clave SOL (no se ve al escribir)")
        if not datos_sfs["clave_sol"]:
            error("sin la clave SOL no se pueden grabar los datos del emisor")
            return False

    if "certificado" in afectados:
        titulo("Certificado")
        datos_sfs["clave_certificado"] = preguntar_clave(
            "Contrasena del certificado (no se ve al escribir)")
        valido, mensaje, _ = contribuyente.revisar_certificado(
            datos["certificado"], datos_sfs["clave_certificado"], datos["ruc"])
        if valido is False:
            error(f"certificado: {mensaje}")
            return False
        (ok if valido else aviso)(f"certificado: {mensaje}")

    if "base" in afectados:
        titulo("Base de datos")
        # Se usa la conexion del archivo —para eso esta— y solo se pide lo unico que
        # el archivo no puede llevar: la contrasena. Volver a preguntar servidor,
        # puerto y base seria pedir de nuevo lo que ya esta escrito ahi.
        db_url = datos["base_datos"]
        if contribuyente.necesita_clave(db_url):
            usuario = contribuyente.usuario_de(db_url)
            nota("         El archivo trae la conexion sin la clave, como corresponde.")
            db_url = contribuyente.con_clave(
                db_url, preguntar_clave(f"Clave de '{usuario}' (no se ve al escribir)"))
        nota(f"         {contribuyente.url_sin_clave(db_url)}")

        listo, mensaje = contribuyente.probar_base(db_url)
        if listo:
            ok(mensaje)
        else:
            error(f"no se pudo conectar: {mensaje}")
            if not confirmar("Cargar la conexion a mano?"):
                return False
            db_url = pedir_base_de_datos()
            if not db_url:
                return False
        # Solo la linea de DATABASE_URL: rehacer el .env entero borraria SOL_CLAVE,
        # que no se puede recuperar de ningun lado.
        respaldo, _ = contribuyente.actualizar_env(RAIZ, {"DATABASE_URL": db_url})
        ok("conexion actualizada en el .env")
        if respaldo:
            nota(f"         respaldo del anterior en {os.path.basename(respaldo)}")

    titulo("Aplicando en el SFS")
    if not contribuyente.esperar_sfs(base_url, segundos=5):
        error("el SFS no responde; levantarlo con 'pm2 start sfs' y reintentar")
        return False

    for nombre, cargar in (("emisor", contribuyente.cargar_emisor),
                           ("direccion", contribuyente.cargar_direccion),
                           ("certificado", contribuyente.cargar_certificado)):
        if nombre not in afectados:
            continue
        listo, mensaje = cargar(base_url, datos_sfs, paso)
        if not listo:
            error(mensaje)
            return False

    ok("actualizado")
    nota("         Reiniciar el daemon para que tome los cambios: pm2 restart facturador")
    return True


# Del 1 al 4 en el orden en que se hacen al instalar por primera vez; despues, con
# una linea en blanco de por medio, lo que se usa cuando la instalacion ya existe.
# Con "actualizar" metido en el medio, quien venia de hacer 1, 2 y 3 leia el 4 como
# el paso siguiente, cuando el que continuaba la secuencia era el de mas abajo.
OPCIONES = (
    ("1", "Instalar el entorno (prerrequisitos, PM2, SFS)", instalar_entorno),
    ("2", "Configurar un cliente", configurar_cliente),
    ("3", "Verificar la instalacion", None),      # se resuelve al importar chequeos
    ("4", "Pasar a produccion", pasar_a_produccion),
    ("5", "Actualizar desde el archivo del cliente", actualizar_desde_archivo),
)

# Antes de que opcion se corta la secuencia de instalacion.
_SEPARADOR = "5"


def main():
    # Habilita los colores ANSI en la consola clasica de Windows.
    if os.name == "nt":
        os.system("")

    import chequeos
    acciones = {c: (t, f) for c, t, f in OPCIONES}
    acciones["3"] = ("Verificar la instalacion", lambda: chequeos.verificar(RAIZ))

    while True:
        print(f"\n{C.NEGRITA}{'=' * 58}{C.FIN}")
        print(f"{C.NEGRITA}  INSTALADOR DEL FACTURADOR SUNAT{C.FIN}")
        print(f"{C.GRIS}  Proyecto en: {RAIZ}{C.FIN}")
        print(f"{C.NEGRITA}{'=' * 58}{C.FIN}\n")
        for codigo, (texto, _) in acciones.items():
            if codigo == _SEPARADOR:
                print()
            print(f"   {C.CYAN}{codigo}{C.FIN}  {texto}")
        print(f"\n   {C.CYAN}0{C.FIN}  Salir\n")

        eleccion = input("  Opcion: ").strip()
        if eleccion == "0":
            return 0
        if eleccion not in acciones:
            aviso("opcion no valida")
            continue
        try:
            acciones[eleccion][1]()
        except KeyboardInterrupt:
            print()
            aviso("interrumpido")
        except Exception as e:
            error(f"{type(e).__name__}: {e}")
        pausa()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  interrumpido")
        sys.exit(1)
