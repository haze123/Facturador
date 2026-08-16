"""
Instalador del Facturador SUNAT.

Se compila a un unico .exe con PyInstaller (ver construir.py), asi que corre en
una PC donde todavia no hay Python: el interprete viaja adentro del ejecutable.

Tres operaciones, las mismas de siempre pero en un solo programa:
  1. Instalar el entorno   (prerrequisitos, PM2, descarga del SFS)
  2. Configurar un cliente (datos del contribuyente, .env, procesos)
  3. Verificar             (diagnostico de una instalacion existente)
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
    aviso("Para una instalacion de PRUEBA, usar una base de prueba, NO la de produccion.")
    nota("  Dos daemons sobre la misma base se pelean los mismos comprobantes.")
    while True:
        db_url = preguntar("DATABASE_URL", "", obligatorio=True)
        listo, mensaje = contribuyente.probar_base(db_url)
        if listo:
            ok(mensaje)
            break
        error(f"no se pudo conectar: {mensaje}")
        if not confirmar("Volver a intentar?"):
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
    base_url = "http://localhost:9000"
    if not contribuyente.esperar_sfs(base_url, segundos=5):
        paso("levantando el SFS...")
        sistema.correr(["pm2", "start", os.path.join(RAIZ, "sfs.config.js"), "--only", "sfs"])
        if not contribuyente.esperar_sfs(base_url, segundos=90):
            error("el SFS no respondio despues de 90 segundos")
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
    respaldo = contribuyente.escribir_env(RAIZ, datos, ruta_sfs)
    if respaldo:
        nota(f"  se respaldo el .env anterior en {os.path.basename(respaldo)}")
    ok(".env escrito y restringido al usuario actual")

    sistema.correr(["pm2", "start", os.path.join(RAIZ, "sfs.config.js")])
    sistema.correr(["pm2", "save"])
    sistema.correr(["pm2-startup", "install"])
    ok("SFS y daemon en PM2, con arranque automatico")

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

OPCIONES = (
    ("1", "Instalar el entorno (prerrequisitos, PM2, SFS)", instalar_entorno),
    ("2", "Configurar un cliente", configurar_cliente),
    ("3", "Verificar la instalacion", None),      # se resuelve al importar chequeos
    ("4", "Pasar a produccion", pasar_a_produccion),
)


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
            print(f"   {C.CYAN}{codigo}{C.FIN}  {texto}")
        print(f"   {C.CYAN}0{C.FIN}  Salir\n")

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
