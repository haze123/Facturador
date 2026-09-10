"""
main.py — Daemon de Facturación Electrónica SUNAT (SFS v2.1)
Gestionar con PM2: pm2 start sfs.config.js --only facturador

Hilos:
  - Hilo Generador : cada INTERVALO_GENERACION_SEG segundos lee la BD de la aplicacion,
                     genera archivos SFS y los envía al facturador local.
  - Hilo CDR       : revisa sobre carpeta RPTA, procesa CDRs al instante.
"""

import base64
import binascii
import json
import logging
import logging.handlers   # submodulo aparte: 'import logging' no lo trae
import os
import re
import sqlite3
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

from dotenv import load_dotenv
import repositorio
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

# Base de datos de la aplicación (PostgreSQL). Se lee la misma DATABASE_URL que usa
# el sistema de SPAXION, para no mantener la conexión declarada en dos lugares.
DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_TIMEOUT_SEG = int(os.getenv("DB_TIMEOUT_SEG", "30"))

# Rutas SFS
SFS_DATA_DIR = p if os.path.exists(p := os.getenv("SFS_DATA_DIR", r"C:\SFS_v-2.1\sunat_archivos\sfs\DATA")) else os.path.join(_BASE, "sunat_archivos", "DATA")
SFS_RPTA_DIR = p if os.path.exists(p := os.getenv("SFS_RPTA_DIR", r"C:\SFS_v-2.1\sunat_archivos\sfs\RPTA")) else os.path.join(_BASE, "sunat_archivos", "RPTA")
# Donde el SFS deja el XML firmado de cada documento. Se deriva de DATA en vez de
# configurarse aparte porque son hermanas dentro de sunat_archivos/sfs: si alguien
# mueve la instalacion, DATA ya trae la ruta nueva y esta la sigue sola.
_SFS_FIRMA_DIR = os.path.join(os.path.dirname(SFS_DATA_DIR), "FIRMA")

SFS_BD_PATH  = os.getenv("SFS_BD_PATH",  r"C:\SFS_v-2.1\bd\BDFacturador.db")
SFS_BASE_URL = os.getenv("SFS_BASE_URL", "http://localhost:9000")

# Configuración del SFS: de acá sale a qué ambiente de SUNAT está enviando
# (RUTA_SERV_CDP), que es donde hay que consultar el ticket de un resumen. Se
# deriva de SFS_DATA_DIR —son carpetas hermanas— para no declarar otra ruta que
# después quede desincronizada.
SFS_CONSTANTES_PATH = os.getenv(
    "SFS_CONSTANTES_PATH",
    os.path.join(os.path.dirname(SFS_DATA_DIR), "VALI", "constantes.properties"),
)

DIR_PROCESADOS = os.path.join(SFS_RPTA_DIR, "procesados")
DIR_ERRORES    = os.path.join(SFS_RPTA_DIR, "errores")

# Emisor override (opcional)
EMISOR_RUC_OVERRIDE = os.getenv("EMISOR_RUC", "").strip()

# Consulta a SUNAT del estado de un comprobante (servicio billConsultService).
# Sirve para saber si un documento llegó cuando se cortó la conexión y no se sabe
# si se envió: si está registrado devuelve su CDR, y si no, recién ahí se reenvía.
# OJO: SUNAT solo publica este servicio en producción — en beta responde 404. Aun
# así consultar no emite nada: es de solo lectura y es independiente del ambiente
# al que el SFS manda los comprobantes.
SUNAT_CONSULTA_URL = os.getenv(
    "SUNAT_CONSULTA_URL",
    "https://e-factura.sunat.gob.pe/ol-it-wsconscpegem/billConsultService",
)
SOL_USUARIO = os.getenv("SOL_USUARIO", "").strip()
SOL_CLAVE   = os.getenv("SOL_CLAVE", "").strip()

# Códigos que confirman que SUNAT NO tiene el comprobante, y por lo tanto habilitan
# a reenviarlo. Es una lista blanca a propósito: verificado contra el servicio real,
# el 0127 es el "no registrado" —aunque su texto diga "El ticket no existe", que es
# un mensaje genérico reutilizado—. Cualquier otro código se toma como incierto.
_CODIGOS_NO_REGISTRADO = ("0127",)

# Códigos con los que SUNAT dice que no pudo traer la constancia, no que el
# comprobante no exista. Verificado contra el catálogo de códigos de SUNAT:
#   0100  El sistema no puede responder su solicitud. Intente nuevamente
#   0125  No se pudo obtener la constancia
#   0126  El ticket no le pertenece al usuario
# Son fallas del lado de SUNAT al recuperar el CDR, así que la consulta se repite más
# tarde en vez de darla por perdida. Ojo con la distinción: que sean transitorios NO
# habilita a reenviar el comprobante —sigue sin saberse si SUNAT lo tiene—, solo a
# volver a preguntar. El único que autoriza el reenvío es el 0127, y vive aparte.
#
# El caso que lo motivó (2026-09-05): F003-006240 recibía 0125 en cada consulta y
# quedaba en 'desconocido' para siempre, repitiendo un WARNING que nadie termina de
# notar, mientras en SUNAT la factura estaba aceptada.
_CODIGOS_CONSULTA_FALLIDA = ("0100", "0125", "0126")

# Cuántas consultas seguidas pueden fallar antes de reportar el comprobante como
# bloqueado. Sin este tope, un servicio caído por días no se distingue de uno que
# tarda un minuto: los dos se ven igual en el log.
MAX_CONSULTAS_FALLIDAS = int(os.getenv("MAX_CONSULTAS_FALLIDAS", "10"))

# Cuántas horas puede un ticket contestar "todavía lo estoy procesando" antes de que
# el aviso escale. SUNAT normalmente tarda minutos, así que 3 horas es holgado de
# sobra; el numero importa por el otro lado, porque un resumen estuvo 24 horas asi
# --con 200 boletas retenidas y ya muerto del lado de SUNAT-- sin que nada lo
# señalara. El max(1, ...) evita que un 0 en el .env convierta cada consulta normal
# en una alarma.
HORAS_TICKET_EN_PROCESO = max(1, int(os.getenv("HORAS_TICKET_EN_PROCESO", "3")))

# Cuánto puede quedarse un CDR en 0 bytes antes de darlo por abandonado. Tiene que
# ser holgado frente a lo que tarda el SFS en escribir un ZIP —segundos— para no
# apartar uno que todavía se está escribiendo.
MINUTOS_CDR_VACIO = int(os.getenv("MINUTOS_CDR_VACIO", "10"))

# Cuánto esperar antes de preguntarle a SUNAT por un comprobante que ya se envió y
# sigue sin CDR. Por debajo de esto lo más probable es que el CDR solo esté demorando.
CONSULTA_SUNAT_TRAS_MIN = int(os.getenv("CONSULTA_SUNAT_TRAS_MIN", "10"))
# Cada cuánto se puede volver a consultar el mismo documento, para no golpear el
# servicio de SUNAT en cada ciclo por algo que sigue igual.
_COOLDOWN_CONSULTA_SEG = 900
_ultima_consulta: dict = {}

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

# Estados SFS que se reintentan. El '10' (rechazado) deja el comprobante sin emitir,
# así que corresponde volver a intentarlo. El '06' ('con errores') se suma porque el
# SFS lo usa para dos cosas distintas: un dato mal armado —que reintentar no arregla—
# y cualquier falla de comunicación con SUNAT ("Hubo un problema al invocar servicio
# SUNAT: Could not send Message."), que sí se resuelve sola cuando vuelve la conexión.
# Sin reintentarlo, un corte de red dejaba comprobantes trabados en DOCUMENTO hasta
# que alguien borraba esas filas a mano. Para el '06' de dato el desenlace no cambia:
# agota los reintentos y termina reportado como bloqueado, igual que antes.
# El '05' NO va acá aunque antes estuviera — es ENVIADO_ANULADO, no un error, y
# reintentarlo significaba reenviar a SUNAT un documento que se había anulado.
_ESTADOS_ERROR = ("06", "10")
# Estados SFS terminales que el daemon NO puede resolver solo: o el SFS generó el XML
# pero no lo envió (boleta de más de 5 días que exige resumen diario, rechazo de
# validación), o el documento quedó anulado. Reintentar no sirve —el resultado sería
# el mismo—, así que se reportan en cada ciclo para que alguien los atienda a mano.
# El '06' y el '10' entran en la lista porque resetear_rechazados() corre ANTES del
# reporte y borra los que todavía tienen reintentos disponibles: si uno de esos dos
# sigue en la tabla al momento de reportar, es porque agotó el tope.
_ESTADOS_BLOQUEADO = ("05", "06", "10")
# Estados en los que el SFS ya cerró el documento con SUNAT: sus archivos de DATA
# no se vuelven a necesitar y hay que borrarlos. Son 5 por comprobante, así que a
# 300 diarios se acumulan 1500 archivos por día en la carpeta que el SFS relee en
# cada pasada.
_ESTADOS_CERRADOS = ("03", "04")

# Estados en los que un resumen sigue en juego: ya salio hacia SUNAT y todavia puede
# resolverse. Lo usan las dos puntas del mismo flujo --la consulta por ticket
# (_resumenes_con_ticket) y el cierre una vez procesado el CDR
# (_cerrar_resumen_en_sfs)--, y viven de una sola constante justamente porque se
# desincronizaron: al ampliar solo la consulta para rescatar los resumenes en '05',
# el cierre siguio exigiendo '08'/'09', asi que un resumen rescatado se consultaba
# para siempre y nunca podia cerrarse. Quedan afuera los cerrados, y tambien el '01'
# y el '02': ahi el resumen todavia no salio, y darlo por aceptado seria mentir.
_ESTADOS_RESUMEN_ABIERTO = ("05", "06", "08", "09", "10")

# Cuántas veces se reenvía un comprobante que SUNAT rechazó. El reenvío manda
# exactamente los mismos datos, así que si el rechazo es por un dato mal armado el
# resultado no cambia: sin tope, el daemon reenvía cada ciclo indefinidamente. Al
# agotarse se reporta como bloqueado y espera corrección manual.
MAX_REINTENTOS_RECHAZO = int(os.getenv("MAX_REINTENTOS_RECHAZO", "3"))

# Motivo (catalogo 09/10) que se le pone a una nota cuya aplicacion no lo guarda.
# Vacio por defecto: sin esto, el daemon NO inventa un motivo y la nota queda sin
# emitir, que es lo correcto cuando el sistema de origen sabe distinguir entre una
# anulacion, un descuento y un ajuste de valor.
#
# Se configura solo donde la aplicacion no puede generar esa diferencia. El caso que
# lo motivo: una pantalla de nota de credito que copia el total y los items del
# comprobante original sin permitir montos parciales, asi que toda nota que puede
# crear es una anulacion completa —"01"— y no hay ambiguedad que resolver. Poner un
# motivo por defecto donde SI se pueden emitir notas parciales es declararle a SUNAT
# algo distinto de lo que paso.
MOTIVO_NOTA_POR_DEFECTO = os.getenv("MOTIVO_NOTA_POR_DEFECTO", "").strip()
# SUNAT rechaza un resumen diario con mas de 500 boletas. El tope por defecto es
# 200 porque es el lote que recomienda el proveedor: un resumen mas chico se firma
# y se acepta mas rapido, y si SUNAT lo observa hay menos boletas que rehacer. Lo
# que sobra no se pierde, va en el resumen del ciclo siguiente.
# El max(1, ...) no es paranoia: con un 0 en el .env el resumen salia vacio, y con
# un negativo descartaba boletas en silencio.
MAX_BOLETAS_RESUMEN = max(1, min(int(os.getenv("MAX_BOLETAS_RESUMEN", "200")), 500))

# Frenos contra el bucle de redeclaración. Visto en produccion el 2026-09-09, tras un
# bloqueo de SUNAT de ~19 horas: 84 resumenes en un dia —lo normal son 2— y 143 boletas
# declaradas 40 veces cada una, sin que nada lo notara ni lo frenara en horas.
#
# El ciclo era: el envio falla sin ticket, se descarta el resumen, las boletas vuelven a
# la cola, se arma otro, SUNAT contesta "2282 - Existe documento ya informado
# anteriormente", y otra vez. Cada vuelta suma un duplicado ante SUNAT, y un duplicado
# solo se deshace con una comunicacion de baja: por eso acá conviene errar por frenar de
# mas. Detenerse y pedir intervencion cuesta una demora; seguir declarando cuesta un
# tramite por cada boleta.
#
# Son dos topes porque atajan el problema en momentos distintos: el de declaraciones
# frena el lote concreto que esta girando en falso, y el diario es la red de seguridad
# por si el bucle aparece de una forma que no previmos.
MAX_DECLARACIONES_BOLETA = max(1, int(os.getenv("MAX_DECLARACIONES_BOLETA", "3")))
MAX_RESUMENES_DIA = max(1, int(os.getenv("MAX_RESUMENES_DIA", "20")))

# Cuanto se conserva la entrada de un resumen ya resuelto en resumenes.json. El margen
# es amplio a proposito: ese archivo es el unico registro de que boletas llevo cada
# resumen, y es lo que permitio reconstruir las 143 del incidente del 2026-09-09.
# Perderlo temprano deja ciego al proximo diagnostico, y lo que se ahorra son kilobytes.
DIAS_RETENCION_RESUMENES = max(1, int(os.getenv("DIAS_RETENCION_RESUMENES", "30")))

# Techo del backoff con el que se reintenta un comprobante trabado por un corte de
# red. No gasta presupuesto de reintentos (ver _es_falla_de_red), así que necesita
# espaciarse solo: sin esto, un corte de dos horas son 120 reenvíos inútiles. Se
# aplana en 15 minutos para que el comprobante salga pronto cuando el servicio
# vuelva, sin quedar esperando media hora de más.
_ESPERA_MAX_RED_MIN = int(os.getenv("ESPERA_MAX_RED_MIN", "15"))

# El contador vive en disco: en memoria, un reinicio de PM2 —que reinicia solo— haría
# arrancar la cuenta de cero y el bucle volvería a ser infinito.
_REINTENTOS_PATH = os.path.join(_BASE, "reintentos.json")
_lock_reintentos = threading.Lock()

# Igual que reintentos.json: el correlativo del resumen y qué boletas lleva cada uno
# viven en disco, porque un reinicio de PM2 no puede repetir un RC-YYYYMMDD-NNN ya
# usado ni perder de vista qué boletas quedaron esperando su CDR.
_RESUMENES_PATH = os.path.join(_BASE, "resumenes.json")
_lock_resumenes = threading.Lock()
# Cuántos bloqueados se detallan en el log antes de resumir; son estables entre
# ciclos y volcarlos todos cada 60s ahoga el resto del log.
_MAX_BLOQUEADOS_LOG = 10
# Tipos que el daemon le entrega al SFS: factura, boleta, nota de credito, nota de
# debito y resumen diario de boletas. Las boletas nunca salen sueltas —van siempre
# por el resumen— asi que el 03 de este set cubre las que el SFS ya tiene en su
# bandeja, no la emision individual. RA (comunicacion de baja) queda fuera: el
# daemon no la emite.
_TIPOS_SFS = {"01", "03", "07", "08", "RC"}

# Constantes.CONSTANTE_TIPO_DOCUMENTO_RBOLETAS: el SFS trata el resumen diario como
# un tipo de documento más, con los mismos dos endpoints REST que todo lo demás
# (GenerarComprobante.htm / enviarXML.htm) y el mismo patrón de dos pasadas. Puertas
# adentro, SUNAT usa un flujo con ticket (sendSummary + getStatus sobre el mismo
# billService) — confirmado contra el WSDL real de producción — pero eso lo resuelve
# el SFS solo: el daemon no necesita hablar SOAP para esto, a diferencia de la
# recuperación de CDR (que sí lo hace directo).
_TIPO_RC = "RC"

# La aplicación guarda el tipo por nombre, no con el código de SUNAT. NOTA_VENTA no
# es un comprobante electrónico —es un documento interno— y por eso no se mapea:
# queda fuera de _TIPOS_SFS y el daemon lo ignora.
_TIPOS_POR_NOMBRE = {
    "FACTURA":        "01",
    "BOLETA":         "03",
    "NOTA_CREDITO":   "07",
    "NOTA_DEBITO":    "08",
}

# Todo se factura gravado al 18%: es lo que corresponde a los servicios de estética.
_FACTOR_IGV = Decimal("1.18")

# La aplicación guarda sus fechas con el reloj de su servidor de BD, que hoy corre
# en UTC; SUNAT en cambio espera la fecha de emisión en hora local del emisor. Sin
# corregir eso, toda venta hecha entre las 19:00 y la medianoche cae en el día
# siguiente y se le declararía a SUNAT una fecha futura, que rechaza.
#
# El desfase se MIDE contra la propia BD en vez de fijarlo, porque no es una
# decisión de este daemon: si alguien cambia la zona horaria del servidor a hora de
# Lima, un -5 fijo quedaría al revés del problema y correría las fechas para el otro
# lado sin que nadie se entere. Medirlo se autocorrige solo.
# Se puede forzar un valor con DESFASE_BD_HORAS (en horas) si hiciera falta.
DESFASE_BD_HORAS = os.getenv("DESFASE_BD_HORAS", "auto").strip().lower()
# Hasta la primera medición se asume lo que hay hoy; la medición ocurre al inicio de
# cada ciclo, antes de que se genere ningún comprobante.
_desfase_horas: float = -5.0
_desfase_medido = False
_lock_desfase = threading.Lock()

if DESFASE_BD_HORAS != "auto":
    try:
        _desfase_horas = float(DESFASE_BD_HORAS)
    except ValueError:
        # Un valor mal escrito no puede pasar por bueno en silencio: sería declarar
        # fechas corridas a SUNAT. Se avisa y se sigue midiendo.
        DESFASE_BD_HORAS = "auto"

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

# PipeResumenBoletaParser del SFS: el .RDI no es una cabecera única sino una línea
# por boleta con este mismo layout de 23 columnas; el .TRD es el desglose de
# tributos de cada línea, 6 columnas, vinculado por posición (idLineaRd = número de
# fila dentro del .RDI, 1-based). Confirmado decompilando el parser, mismo método
# que para notas y ND.
_COLS_RDI = 23
_COLS_TRD = 6

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
# Comprobante.tipoNota, nunca se infiere.
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

# El SFS no registra un resumen diario en su propia bandeja apenas responde EXITO a
# GenerarComprobante.htm: lo escanea un job interno aparte, que tardó hasta ~90s en
# la práctica. Sin este resguardo, _boletas_en_resumenes_activos() no veía el
# resumen recién generado durante ese lapso y el siguiente ciclo (60s) generaba un
# segundo resumen con las mismas boletas — confirmado en beta: dos RC duplicados
# con el mismo pool de 5 boletas antes de que el primero apareciera en la bandeja.
_GRACIA_REGISTRO_RC_SEG = 300
_ultimo_intento: dict = {}

# Pausas al conversar con el SFS. No son arbitrarias: el facturador procesa los
# archivos de DATA en background, así que hay que darle tiempo entre el pedido de
# generación y el de envío o responde "No existen datos que procesar".
_ESPERA_XML_SEG        = 2   # tras pedir la generación del XML
_ESPERA_REINTENTO_SEG  = 3   # antes de reintentar un envío que falló
_ESPERA_ENTRE_DOCS_SEG = 1   # para no saturar al SFS documento tras documento

# Estado de la columna Comprobante.enviado, que es boolean: no admite un estado
# intermedio. True significa "SUNAT devolvió un CDR de aceptación" y lo escriben
# los adaptadores por su cuenta; el "entregado al SFS, esperando CDR" no se guarda
# acá, se deduce de la BD del SFS (ver _docs_en_vuelo()).
ENVIADO_PENDIENTE = False   # por generar / reintentar

# Estados de CDR que dan por buena la emisión (SUNAT acepta con y sin observaciones)
_CDR_ACEPTADOS = {"ACEPTADO", "OBSERVADO"}

# Recorte defensivo del motivo antes de guardarlo en Comprobante.errors. La columna
# es text y no tiene límite, pero un mensaje enorme de SUNAT no aporta nada.
_MAX_ERRORS_SQL = 4000

# DOCUMENTO.DES_OBSE del SFS es VARCHAR(250). SQLite no lo hace cumplir, pero la
# aplicacion Java si lo lee con ese ancho: pasarse es arriesgarse a que lo corte de
# una forma que no controlamos.
_MAX_DES_OBSE = 250

# Serializa el barrido de RPTA: watchdog lanza una llamada por cada CDR que aparece
# y todas recorren el directorio completo (ver procesar_respuestas).
_lock_cdr = threading.Lock()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

# El log rota a los 5 MB y se conservan 5 archivos: unos 25 MB en total. Es el
# registro de qué pasó con cada comprobante, así que hay que poder mirar atrás
# —un rechazo puede investigarse semanas después—, pero sin que crezca sin
# límite en una PC que va a estar años emitiendo.
LOG_MAX_MB       = int(os.getenv("LOG_MAX_MB", "5"))
LOG_ARCHIVOS     = int(os.getenv("LOG_ARCHIVOS", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            os.path.join(_BASE, "facturador.log"),
            maxBytes=LOG_MAX_MB * 1024 * 1024,
            backupCount=LOG_ARCHIVOS,
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


def _tipo_sunat(valor) -> str:
    """
    Código de comprobante de SUNAT a partir de lo que guarda la aplicación, que usa
    nombres ('BOLETA') en vez de códigos. Si ya viene un código, se deja pasar.
    """
    texto = _texto(valor).upper()
    if not texto:
        return ""
    return _TIPOS_POR_NOMBRE.get(texto, _codigo(texto))


def _base_e_igv(total):
    """
    Separa un importe con IGV incluido en base imponible e impuesto.

    La aplicación solo guarda el total cobrado. Se asume todo gravado al 18%, que es
    lo que corresponde a los servicios de estética; un ítem exonerado o gratuito
    necesitaría el tipo de afectación, que la base no tiene (ver README).
    """
    bruto = formatear_decimal(total)
    base = (bruto / _FACTOR_IGV).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(base), float(bruto - base)


def _desglosar_igv(precio_unitario, cantidad, total_linea):
    """(valor unitario sin IGV, valor de venta de la línea, IGV de la línea)."""
    # Los dos factores pasan por formatear_decimal, como ya hacia la linea de abajo:
    # en SQL Server 'cantidad' es nvarchar, y multiplicar texto por un float reventaba
    # el ciclo entero con TypeError cuando la linea no traia total.
    if total_linea is not None:
        total = total_linea
    else:
        total = float(formatear_decimal(precio_unitario, 6)
                      * (formatear_decimal(cantidad, 6) or Decimal("1")))
    valor_venta, igv = _base_e_igv(total)
    cant = formatear_decimal(cantidad) or Decimal("1")
    unitario = (Decimal(str(valor_venta)) / cant) if cant else Decimal("0")
    return float(unitario), valor_venta, igv


def formatear_decimal(valor, decimales: int = 2) -> Decimal:
    """
    Importe redondeado, con 2 decimales salvo que se pidan otros.

    El valor unitario es el único campo que necesita más: se declara con 6 porque
    SUNAT verifica que cantidad × valor unitario cuadre con el valor de venta, y
    con 2 decimales la cuenta no cierra. Un servicio de S/10 en 3 unidades da
    2.823333 por unidad; redondeado a 2.82, tres unidades suman 8.46 contra los
    8.47 declarados como valor de venta.
    """
    if valor is None:
        return Decimal(0).quantize(Decimal(1).scaleb(-decimales))
    try:
        return Decimal(str(valor)).quantize(Decimal(1).scaleb(-decimales),
                                            rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(0).quantize(Decimal(1).scaleb(-decimales))


_UNIDADES = ("", "UNO", "DOS", "TRES", "CUATRO", "CINCO", "SEIS", "SIETE", "OCHO", "NUEVE",
             "DIEZ", "ONCE", "DOCE", "TRECE", "CATORCE", "QUINCE", "DIECISEIS", "DIECISIETE",
             "DIECIOCHO", "DIECINUEVE", "VEINTE")
_DECENAS  = ("", "", "VEINTI", "TREINTA", "CUARENTA", "CINCUENTA", "SESENTA", "SETENTA",
             "OCHENTA", "NOVENTA")
_CENTENAS = ("", "CIENTO", "DOSCIENTOS", "TRESCIENTOS", "CUATROCIENTOS", "QUINIENTOS",
             "SEISCIENTOS", "SETECIENTOS", "OCHOCIENTOS", "NOVECIENTOS")
_NOMBRE_MONEDA = {"PEN": "SOLES", "USD": "DOLARES AMERICANOS", "EUR": "EUROS"}


def _centenas_a_letras(n: int) -> str:
    if n == 100:
        return "CIEN"
    partes = []
    if n >= 100:
        partes.append(_CENTENAS[n // 100])
        n %= 100
    if n <= 20:
        if n:
            partes.append(_UNIDADES[n])
    elif n < 30:
        # 21..29 se escriben juntos: VEINTIUNO, VEINTIDOS, ...
        partes.append(_DECENAS[2] + _UNIDADES[n % 10])
    else:
        partes.append(_DECENAS[n // 10] + (f" Y {_UNIDADES[n % 10]}" if n % 10 else ""))
    return " ".join(p for p in partes if p)


def numero_a_letras(monto, moneda: str = "PEN") -> str:
    """
    Importe en palabras, como lo exige SUNAT en la leyenda 1000 del comprobante.

    La aplicación no guarda este texto, así que se arma acá. El formato es el usual
    en Perú: "CIENTO DIECIOCHO CON 00/100 SOLES".
    """
    valor = formatear_decimal(monto)
    entero = int(valor)
    centavos = int((valor - entero) * 100)

    if entero == 0:
        letras = "CERO"
    else:
        bloques = []
        millones, resto = divmod(entero, 1_000_000)
        miles, unidades = divmod(resto, 1000)
        if millones:
            bloques.append("UN MILLON" if millones == 1 else f"{_centenas_a_letras(millones)} MILLONES")
        if miles:
            bloques.append("MIL" if miles == 1 else f"{_centenas_a_letras(miles)} MIL")
        if unidades:
            bloques.append(_centenas_a_letras(unidades))
        letras = " ".join(bloques)

    return f"{letras} CON {centavos:02d}/100 {_NOMBRE_MONEDA.get(_texto(moneda, 'PEN').upper(), 'SOLES')}"


def formatear_fecha_hora(fecha_raw) -> datetime:
    """
    La fecha tal como está guardada, sin mover la hora. Para lo que se le declara a
    SUNAT hay que pasarla antes por fecha_local(): lo que hay en la BD está en UTC.
    """
    if isinstance(fecha_raw, datetime):
        return fecha_raw
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(fecha_raw).strip(), fmt)
        except (ValueError, AttributeError):
            pass
    raise ValueError(f"Fecha inválida: {fecha_raw!r}")


def detectar_desfase_bd(conn) -> float:
    """
    Mide cuánto adelanta el reloj de la BD respecto de la hora local y lo recuerda.

    Es lo que hace que el daemon siga funcionando si alguien cambia la zona horaria
    del servidor: con la BD en UTC da -5, y si pasa a hora de Lima da 0, sin tocar
    nada acá. Se redondea a media hora porque ninguna zona horaria usa una
    granularidad menor, y así un par de segundos de latencia no ensucian el valor.
    """
    global _desfase_horas
    if DESFASE_BD_HORAS != "auto":
        return _desfase_horas
    try:
        filas = _bd().reloj(conn)
        # Se compara la lectura "de pared" del servidor contra el reloj local.
        pared = filas[0]["con_zona"].replace(tzinfo=None)
        crudo = (pared - datetime.now()).total_seconds() / 3600
        medido = round(crudo * 2) / 2
    except Exception:
        logger.exception("No se pudo medir el desfase horario de la BD; se conserva %+g h.",
                         _desfase_horas)
        return _desfase_horas

    global _desfase_medido
    correccion = -medido
    with _lock_desfase:
        if correccion != _desfase_horas or not _desfase_medido:
            logger.info(
                "El reloj de la BD adelanta %+g h respecto de la hora local; las fechas "
                "de emisión se corrigen en %+g h antes de declararlas a SUNAT.",
                medido, correccion,
            )
            _desfase_horas = correccion
            _desfase_medido = True
    return _desfase_horas


def fecha_local(fecha_raw) -> datetime:
    """
    Fecha de la BD llevada a la hora local del emisor.

    Es la única forma válida de leer una fecha de emisión: es la que va al
    comprobante y la que decide a qué día pertenece una boleta para el resumen
    diario. Ver detectar_desfase_bd().
    """
    fecha = formatear_fecha_hora(fecha_raw)
    if fecha.tzinfo is not None:
        # Si alguna vez llega con zona horaria explícita, se convierte de verdad en
        # vez de sumarle el desplazamiento a ciegas.
        return fecha.astimezone(timezone(timedelta(hours=_desfase_horas))).replace(tzinfo=None)
    return fecha + timedelta(hours=_desfase_horas)


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
# Base de datos — PostgreSQL de la aplicación
# ---------------------------------------------------------------------------

def _url_sin_clave(url: str) -> str:
    """La URL de conexión sin la contraseña, para poder mostrarla en el log."""
    return repositorio.url_sin_clave(url)


def _bd():
    """
    El adaptador del motor que indique DATABASE_URL.

    El daemon no sabe con qué base está hablando: pide siempre lo mismo y cada
    adaptador traduce a su esquema y su dialecto (ver repositorio/).
    """
    if not DATABASE_URL:
        raise RuntimeError("Falta DATABASE_URL en el .env")
    return repositorio.elegir(DATABASE_URL)


def conectar_bd():
    return _bd().conectar(DATABASE_URL, DB_TIMEOUT_SEG)


def _escribir_bd(operacion, *args) -> int:
    """
    Ejecuta una escritura del adaptador. Devuelve las filas afectadas, o -1 si falló.

    El try va acá y no en cada adaptador para que un motor nuevo no tenga que
    acordarse de replicar el manejo de errores.
    """
    try:
        return operacion(*args)
    except Exception:
        logger.exception("Error escribiendo en la BD (%s)", getattr(operacion, "__name__", "?"))
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
    """
    Datos del emisor. La aplicación no tiene tabla de emisores —solo guarda el nombre
    en Configuracion—, así que el RUC sale de EMISOR_RUC en el .env.
    """
    razon = _texto(_bd().emisor(conn))
    if not EMISOR_RUC_OVERRIDE:
        return None
    return {"ruc": EMISOR_RUC_OVERRIDE, "razon_social": razon}


def obtener_receptor(conn, factura_id):
    """
    Receptor del comprobante. Cada esquema lo guarda distinto —uno separa tipo y
    número de documento, otro los deduce del RUC o el DNI—, así que la traducción vive
    en el adaptador y acá llega ya normalizado.
    """
    if not factura_id:
        return {}
    return _bd().receptor(conn, factura_id) or {}


def obtener_items(conn, factura_id):
    """
    Ítems del comprobante, con el desglose de IGV que la aplicación no guarda.

    FacturaItem tiene columnas para el desglose (valor, valorVenta, igvVenta, precio)
    pero la aplicación solo llena nombre, cantidad, precioUnit y total. Cuando faltan
    se calculan desde el precio con IGV incluido; si algún día empieza a llenarlas,
    se respetan las suyas.
    """
    filas = _bd().items(conn, factura_id)
    items = []
    for f in filas:
        cantidad = f["dec_cantidad"] or f["cantidad"] or 1
        precio_unit = f["precio"] if f["precio"] is not None else f["precio_unit"]
        valor_unit, valor_venta, igv_venta = _desglosar_igv(precio_unit, cantidad, f["total"])
        items.append({
            "descripcion":     f["descripcion"],
            "codigo_producto": f["codigo_producto"],
            # ZZ = "servicio" en el catálogo 03 de SUNAT. Si el esquema del cliente
            # trae su propia unidad de medida (un grifo factura galones), se respeta.
            "medida":          f.get("medida") or "ZZ",
            "dec_cantidad":    cantidad,
            "valor":           f["valor"]     if f["valor"]     is not None else valor_unit,
            "valor_venta":     valor_venta,
            "igv_venta":       f["igv_venta"] if f["igv_venta"] is not None else igv_venta,
            "precio":          precio_unit,
        })
    return items


# Comprobantes ya reportados como incompletos. Una venta a la que le falta un dato
# se queda así hasta que alguien la corrija, y repetir el aviso en cada ciclo llenaría
# el log de la misma línea cada 60 segundos. Se avisa una vez por corrida: si sigue
# sin resolverse, vuelve a aparecer en el próximo arranque.
_avisados_incompletos: set = set()


def _avisar_incompleto(clave, mensaje: str, *args):
    if clave in _avisados_incompletos:
        return
    _avisados_incompletos.add(clave)
    logger.warning(mensaje, *args)


def obtener_comprobantes_pendientes(conn):
    """
    Comprobantes por emitir, con los nombres de campo que espera el resto del daemon.

    La aplicación guarda el tipo como texto ('BOLETA', 'FACTURA') y no el código de
    SUNAT, y deja en NULL el desglose de importes: ambas cosas se resuelven acá para
    que procesar_comprobante() reciba siempre lo mismo, venga de donde venga.

    Las boletas (03) quedan afuera a propósito: van por el resumen diario
    (ver obtener_boletas_para_resumen/generar_resumen_diario), nunca individualmente.
    """
    # Los datos del comprobante viven en Factura: la tabla Comprobante se fusionó
    # dentro de ella, así que "id" y "factura_id" son la misma fila (se repite el
    # nombre solo porque obtener_receptor() y obtener_items() esperan esa clave).
    # Las filas sin numeración NO se filtran acá: una venta cobrada a la que la
    # aplicación nunca le asignó número igual no se puede emitir, pero descartarla en
    # el SQL la hacía desaparecer sin una sola línea en el log. Pasa a la validación,
    # que la reporta identificándola por su id.
    filas = _bd().pendientes(conn)
    pendientes = []
    for f in filas:
        tipo_comp = _tipo_sunat(f["tipo_comprobante"] or f["tipo_enum"])
        if tipo_comp == "03":
            continue
        gravadas, igv = f["gravadas"], f["igv"]
        if gravadas is None or igv is None:
            gravadas, igv = _base_e_igv(f["total"])
        pendientes.append({
            "id":                             f["id"],
            "factura_id":                     f["id"],
            "tipo_comprobante":               tipo_comp,
            "numeracion_comprobante":         f["numeracion_comprobante"],
            "fecha_emision":                  f["fecha_emision"],
            "tipo_moneda":                    f["tipo_moneda"],
            "tipo_nota":                      f["tipo_nota"],
            "tipo_documento_afectado":        _tipo_sunat(f["tipo_documento_afectado"]),
            "numeracion_documento_afectado":  f["numeracion_documento_afectado"],
            "motivo_documento_afectado":      f["motivo_documento_afectado"],
            "gravadas":                       gravadas,
            "igv":                            igv,
            "total":                          f["total"],
            "monto_letras": _texto(f["monto_letras"]) or numero_a_letras(f["total"], f["tipo_moneda"]),
        })
    return pendientes


def obtener_boletas_para_resumen(conn) -> list:
    """
    Boletas sin enviar, emitidas antes de hoy: el pool de candidatas para el próximo
    resumen diario. Las de hoy se dejan para el resumen de un día siguiente — recién
    "cerraron" su día una vez que termina, y mandar un resumen a medio día se presta
    a que lleguen más boletas después y queden fuera.
    """
    filas = _bd().pendientes(conn)
    hoy = datetime.now().date()
    candidatas = []
    for f in filas:
        if _tipo_sunat(f["tipo_comprobante"] or f["tipo_enum"]) != "03":
            continue
        faltantes = _validar_campos_obligatorios({
            "numeracion_comprobante": f["numeracion_comprobante"],
            "fecha_emision":          f["fecha_emision"],
            "total":                  f["total"],
        })
        if faltantes:
            _avisar_incompleto(
                f["id"],
                "Boleta %s sin datos obligatorios (%s); no entra al resumen hasta completarlos.",
                f["numeracion_comprobante"] or f["id"], ", ".join(faltantes),
            )
            continue
        # Una boleta que un resumen ya excluyó MAX_REINTENTOS_RECHAZO veces por venir
        # con código de línea no se vuelve a proponer sola: seguiría chocando con el
        # mismo dato observado. Ver _procesar_lineas_de_resumen().
        if _reintentos_de(f["numeracion_comprobante"]) >= MAX_REINTENTOS_RECHAZO:
            _avisar_incompleto(
                f["id"],
                "Boleta %s agotó los reenvíos dentro de un resumen; no se reincluye "
                "hasta que se corrija el dato observado.",
                f["numeracion_comprobante"] or f["id"],
            )
            continue
        # En hora local, que es la que define a qué día pertenece la boleta: en UTC
        # una boleta de las 20:00 figuraría como del día siguiente y nunca entraría.
        if fecha_local(f["fecha_emision"]).date() >= hoy:
            continue
        gravadas, igv = f["gravadas"], f["igv"]
        if gravadas is None or igv is None:
            gravadas, igv = _base_e_igv(f["total"])
        candidatas.append({
            "id":                     f["id"],
            "factura_id":             f["id"],
            "numeracion_comprobante": f["numeracion_comprobante"],
            "fecha_emision":          f["fecha_emision"],
            "gravadas":               gravadas,
            "igv":                    igv,
            "total":                  f["total"],
            "monto_letras": _texto(f["monto_letras"]) or numero_a_letras(f["total"], "PEN"),
        })
    return candidatas

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
    falta algo. El código de motivo NO se deduce: es un dato tributario y una nota con
    el motivo equivocado es una declaración incorrecta ante SUNAT. Sin él la nota no se
    emite y queda reportada para que la completen.

    La única forma de rellenarlo es que alguien lo declare explícitamente en
    MOTIVO_NOTA_POR_DEFECTO, y eso solo tiene sentido donde la aplicación de origen no
    puede generar más de un tipo de nota (ver el comentario de esa constante).
    """
    cod_motivo   = _codigo(comp.get("tipo_nota"))
    tip_afectado = _codigo(comp.get("tipo_documento_afectado"))
    num_afectado = _campo_pipe(comp.get("numeracion_documento_afectado"))

    if not cod_motivo and MOTIVO_NOTA_POR_DEFECTO:
        candidato = _codigo(MOTIVO_NOTA_POR_DEFECTO)
        catalogo = _MOTIVOS_NOTA.get(tipo_comp, {})
        if candidato in catalogo:
            cod_motivo = candidato
            # A nivel INFO y no WARNING: acá el motivo por defecto es la configuración
            # esperada, no una anomalía. Pero se registra en cada nota, porque queda
            # declarado ante SUNAT y tiene que poder rastrearse cuál salió así.
            logger.info(
                "Nota %s-%s sin tipo_nota; se usa el motivo por defecto %s (%s).",
                tipo_comp, num_comp, cod_motivo, catalogo[candidato],
            )
        else:
            # Un error de tipeo en el .env no puede convertirse en una declaración
            # con un motivo que no existe: se ignora y la nota queda sin emitir, que
            # es el comportamiento de siempre cuando falta el dato.
            logger.error(
                "MOTIVO_NOTA_POR_DEFECTO=%r no es un motivo válido para el tipo %s "
                "(catálogo: %s); se ignora y la nota no se emite.",
                MOTIVO_NOTA_POR_DEFECTO, tipo_comp, ", ".join(sorted(catalogo)) or "ninguno",
            )

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


def _validar_campos_obligatorios(comp: dict) -> list:
    """
    Campos sin los que no se puede armar un comprobante ni una línea del resumen
    diario. Solo devuelve qué falta —no decide qué hacer con eso—, para que sirva
    tanto a procesar_comprobante() como a obtener_boletas_para_resumen(): cada
    camino de emisión define si bloquea del todo o solo excluye esa fila.

    No repite lo que ya filtra la consulta SQL (numeracionComprobante IS NOT NULL);
    igual se valida acá porque es la única garantía si algún día una fila llega por
    otro camino, y porque una fecha ilegible pasaba hoy como una excepción genérica
    sin motivo claro en el log.
    """
    faltantes = []
    if not _texto(comp.get("numeracion_comprobante")):
        faltantes.append("numeracion_comprobante")
    try:
        formatear_fecha_hora(comp.get("fecha_emision"))
    except (ValueError, TypeError):
        faltantes.append("fecha_emision")
    if comp.get("total") is None:
        faltantes.append("total")
    return faltantes


def _linea_detalle(item: dict) -> str:
    """Una línea del archivo .det: las 36 columnas en el orden que lee el SFS."""
    cant   = formatear_decimal(item.get("dec_cantidad") or item.get("cantidad_venta") or item.get("cantidad", 1))
    # Con 6 decimales, no 2: es lo que hace cuadrar cantidad × valor unitario
    # contra el valor de venta, que es lo que SUNAT verifica.
    v_unit = formatear_decimal(item.get("valor"), 6)
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

    # Validación previa: todo esto se chequea antes de escribir nada, para no dejar
    # archivos huérfanos en DATA por un comprobante que igual no se puede armar bien.
    faltantes = _validar_campos_obligatorios(comp)
    if faltantes:
        _avisar_incompleto(
            comp.get("factura_id") or num_comp,
            "Comprobante %s-%s sin datos obligatorios (%s); no se emite hasta completarlos.",
            tipo_comp, num_comp or "?", ", ".join(faltantes),
        )
        return False

    items = obtener_items(conn, comp.get("factura_id"))
    if not items:
        logger.warning(
            "Comprobante %s-%s sin ítems; no se emite hasta completarlos.",
            tipo_comp, num_comp,
        )
        return False

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

    receptor     = obtener_receptor(conn, comp.get("factura_id"))
    tipo_doc_rec = _campo_pipe(receptor.get("tipo_documento"),   "0")
    num_doc_rec  = _campo_pipe(receptor.get("numero_documento"), "00000000")
    razon_social = _campo_pipe(receptor.get("razon_social"),     "CLIENTE VARIOS")
    moneda       = _campo_pipe(comp.get("tipo_moneda"),          "PEN")
    monto_letras = _campo_pipe(comp.get("monto_letras"),         "SIN DESCRIPCION")

    # En hora local: es la fecha que se le declara a SUNAT (la BD guarda UTC).
    fecha_dt  = fecha_local(comp.get("fecha_emision"))
    fecha_str = fecha_dt.strftime("%Y-%m-%d")
    hora_str  = fecha_dt.strftime("%H:%M:%S")

    tot_grav  = formatear_decimal(comp.get("gravadas")  or comp.get("total_gravadas"))
    tot_igv   = formatear_decimal(comp.get("igv")       or comp.get("total_igv"))
    tot_venta = formatear_decimal(comp.get("total")     or comp.get("total_venta"))

    lineas_det = [_linea_detalle(item) for item in items]

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

    escribir_archivo(rutas["det"], "".join(lineas_det))

    # OJO: no se toca Comprobante.enviado acá. Generar los archivos no es haber
    # enviado nada; el estado lo mueve ciclo_generacion() recién cuando el SFS
    # confirma la recepción, y lo cierra el CDR de SUNAT.
    logger.info("Archivos SFS generados: %s", num_comp)
    return True


def _linea_rdi(fecha_emision: str, fecha_resumen: str, boleta: dict, receptor: dict) -> str:
    """
    Una línea del .RDI: PipeResumenBoletaParser no lee una cabecera única sino una
    línea por boleta con este mismo layout de 23 columnas.

    Dos campos que parecen intercambiables y no lo son (verificado en
    ConvertirRBoletasXML.ftl, que es lo que arma el XML final):
      - tipDocResumen -> <cbc:DocumentTypeCode>: el TIPO de comprobante, "03" para
        una boleta. Poner "1" acá lo rechaza SUNAT con el error 2241.
      - tipEstado     -> <cbc:ConditionCode>: el estado de la línea, "1" = nueva.

    Los bloques de documento modificado y de percepción son opcionales, y la
    plantilla los emite con `<#if serDocModifico != "">` / `<#if tipRegPercepcion
    != "">`: la condición es contra CADENA VACÍA, no contra "-". Un "-" ahí los
    daría por presentes y armaría un XML con esos nodos rellenos de basura, así que
    esos 8 campos van vacíos. Es lo contrario de lo que hace el resto de los
    archivos del daemon, donde "-" es el relleno habitual.
    """
    tipo_doc_rec = _campo_pipe(receptor.get("tipo_documento"), "0")
    num_doc_rec  = _campo_pipe(receptor.get("numero_documento"), "00000000")
    grav  = formatear_decimal(boleta["gravadas"])
    total = formatear_decimal(boleta["total"])
    campos = [
        fecha_emision, fecha_resumen, "03", boleta["numeracion_comprobante"],
        tipo_doc_rec, num_doc_rec, "PEN",
        f"{grav:.2f}", "0.00", "0.00", "0.00", "0.00", "0.00", f"{total:.2f}",
        "", "", "", "",
        "", "", "", "",
        "1",
    ]
    # Una columna de más o de menos hace que el SFS rechace el archivo entero con
    # un mensaje que no dice cuál falta; mejor que salte acá.
    if len(campos) != _COLS_RDI:
        raise ValueError(f".RDI: {len(campos)} columnas, se esperan {_COLS_RDI}")
    return "|".join(campos) + "|\n"


def _linea_trd(id_linea: int, boleta: dict) -> str:
    """
    Desglose de tributos de una línea del .RDI: 6 columnas, mismo patrón que el .tri
    de un comprobante individual. id_linea es la posición (1-based) de la boleta
    dentro del .RDI: es lo único que vincula ambos archivos, porque el parser no
    guarda un identificador propio por línea.
    """
    grav = formatear_decimal(boleta["gravadas"])
    igv  = formatear_decimal(boleta["igv"])
    campos = [str(id_linea), "1000", "IGV", "VAT", f"{grav:.2f}", f"{igv:.2f}"]
    if len(campos) != _COLS_TRD:
        raise ValueError(f".TRD: {len(campos)} columnas, se esperan {_COLS_TRD}")
    return "|".join(campos) + "|\n"


def generar_resumen_diario(conn, ruc_emisor: str):
    """
    Agrupa en un solo resumen las boletas pendientes de días anteriores y escribe
    sus .RDI/.TRD en DATA. Devuelve el doc {"num_ruc","tip_docu","num_docu"} listo
    para activar_procesamiento_sfs(), o None si no había boletas candidatas.
    """
    boletas = obtener_boletas_para_resumen(conn)
    if not boletas:
        return None
    excluidas = _boletas_en_resumenes_activos(ruc_emisor)
    boletas = [b for b in boletas if b["numeracion_comprobante"] not in excluidas]
    if not boletas:
        return None

    # Un resumen declara UNA sola fecha de referencia (<cbc:ReferenceDate> en
    # ConvertirRBoletasXML.ftl del SFS), asi que no puede mezclar dias: se toma el
    # mas antiguo pendiente y los demas esperan al proximo ciclo.
    boletas.sort(key=lambda b: (fecha_local(b["fecha_emision"]).date(),
                               b["numeracion_comprobante"]))
    dia = fecha_local(boletas[0]["fecha_emision"]).date()
    del_dia = [b for b in boletas if fecha_local(b["fecha_emision"]).date() == dia]
    boletas, restantes = del_dia[:MAX_BOLETAS_RESUMEN], len(del_dia) - MAX_BOLETAS_RESUMEN
    if restantes > 0:
        logger.info(
            "%s tiene %d boleta(s) pendientes; entran %d en este resumen y %d en el siguiente.",
            dia, len(del_dia), len(boletas), restantes,
        )

    hoy = datetime.now()
    numeraciones = [b["numeracion_comprobante"] for b in boletas]

    # Antes de armar nada: si este mismo lote ya viene girando en falso, frenar. Sin
    # esto el ciclo armó, mandó y descartó 84 resúmenes en un día (2026-09-09),
    # declarando las mismas boletas una y otra vez ante SUNAT.
    motivo = _motivo_para_frenar(numeraciones, hoy.strftime("%Y%m%d"))
    if motivo:
        logger.error(
            "NO se genera el resumen diario: %s. REQUIERE REVISIÓN MANUAL: verificar en "
            "el portal de SUNAT cuáles de esas boletas ya están declaradas antes de "
            "volver a intentarlo; cada intento de más es un duplicado que solo se "
            "deshace con una comunicación de baja.", motivo,
        )
        return None

    fecha_resumen = hoy.strftime("%Y-%m-%d")
    numeracion_rc = _siguiente_numeracion_rc(hoy.strftime("%Y%m%d"))
    base = _nombre_archivo_rc(ruc_emisor, numeracion_rc)
    os.makedirs(SFS_DATA_DIR, exist_ok=True)

    lineas_rdi, lineas_trd = [], []
    for i, boleta in enumerate(boletas, start=1):
        receptor = obtener_receptor(conn, boleta.get("factura_id"))
        fecha_emision = fecha_local(boleta["fecha_emision"]).strftime("%Y-%m-%d")
        lineas_rdi.append(_linea_rdi(fecha_emision, fecha_resumen, boleta, receptor))
        lineas_trd.append(_linea_trd(i, boleta))

    escribir_archivo(os.path.join(SFS_DATA_DIR, f"{base}.RDI"), "".join(lineas_rdi))
    escribir_archivo(os.path.join(SFS_DATA_DIR, f"{base}.TRD"), "".join(lineas_trd))

    _registrar_resumen(numeracion_rc, numeraciones)
    # Con un tope de 200 la lista entera hacia una linea de log de miles de
    # caracteres por resumen. El detalle completo vive en resumenes.json.
    muestra = ", ".join(numeraciones[:_MAX_BLOQUEADOS_LOG])
    if len(numeraciones) > _MAX_BLOQUEADOS_LOG:
        muestra += f" ... y {len(numeraciones) - _MAX_BLOQUEADOS_LOG} mas"
    logger.info(
        "Resumen diario %s generado con %d boleta(s): %s",
        numeracion_rc, len(numeraciones), muestra,
    )
    return {"num_ruc": ruc_emisor, "tip_docu": _TIPO_RC, "num_docu": numeracion_rc}

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


def sincronizar_bandeja_sfs() -> bool:
    """
    Fuerza al SFS a releer la carpeta DATA y registrar en su bandeja lo que haya
    nuevo. Devuelve True si respondió.

    Hace falta porque el SFS solo escanea DATA cuando la pantalla de su bandeja
    hace su refresco periódico (cargarArchivosContribuyente cuelga de
    ActualizarPantalla.htm, NO de CargarPantalla.htm —la carga inicial de la
    pantalla— pese a lo que sugiere el nombre) o desde un job programado que
    exige el temporizador prendido. Ni GenerarComprobante.htm ni enviarXML.htm
    lo hacen: operan sobre lo que ya está en la bandeja.

    Confirmado en la práctica: con CargarPantalla.htm el daemon llamaba a este
    endpoint cada ciclo sin ningún efecto —cero cargarArchivosContribuyente en
    el log del SFS durante 46 minutos seguidos— y el escaneo solo corría cuando
    alguien tenía la bandeja abierta en el navegador, porque es esa página la
    que dispara ActualizarPantalla.htm en su refresco automático. Sin este
    endpoint (el correcto) el daemon dependía de esa pestaña, y si quedaba en
    segundo plano el navegador le frenaba el temporizador y los documentos se
    quedaban sin procesar hasta que alguien volvía a tocar la PC.
    """
    r = _sfs_post("api/ActualizarPantalla.htm", {})
    if r is None:
        return False
    if r.get("validacion") != "EXITO":
        logger.warning("El SFS no pudo releer DATA: %s", _resumen_sfs(r))
        return False
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

    # Que el SFS levante de DATA lo recién escrito antes de pedirle nada sobre ello:
    # los endpoints de generar y enviar solo ven lo que ya está en su bandeja.
    sincronizar_bandeja_sfs()

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
# Consulta directa a SUNAT — ¿el comprobante ya está registrado?
# ---------------------------------------------------------------------------

# Plantilla del sobre SOAP. La autenticación va como UsernameToken de WS-Security y
# el usuario es el RUC pegado al usuario SOL secundario, sin separador.
_SOBRE_CONSULTA = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ser="http://service.sunat.gob.pe">
  <soapenv:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>{usuario}</wsse:Username>
        <wsse:Password>{clave}</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </soapenv:Header>
  <soapenv:Body>
    <ser:getStatusCdr>
      <rucComprobante>{ruc}</rucComprobante>
      <tipoComprobante>{tipo}</tipoComprobante>
      <serieComprobante>{serie}</serieComprobante>
      <numeroComprobante>{numero}</numeroComprobante>
    </ser:getStatusCdr>
  </soapenv:Body>
</soapenv:Envelope>"""


def _texto_de_nodo(xml: str, etiqueta: str) -> str:
    """Contenido de un nodo de la respuesta SOAP, sin importar su prefijo."""
    m = re.search(rf"<(?:\w+:)?{etiqueta}>(.*?)</(?:\w+:)?{etiqueta}>", xml, re.S)
    return m.group(1).strip() if m else ""


def _codigo_de_fault(cuerpo: str) -> str:
    """
    Código de SUNAT dentro del faultcode de un error SOAP.

    Viene pegado al espacio de nombres —"soap-env:Client.0127"— y es lo único que
    distingue un rechazo con veredicto de una falla pasajera. Sin extraerlo, los dos
    llegaban como None a quien llama y no habia forma de saber si convenia reintentar
    o si SUNAT ya habia dicho la ultima palabra.
    """
    m = re.search(r"<(?:\w+:)?faultcode>(.*?)</(?:\w+:)?faultcode>", cuerpo, re.S)
    if not m:
        return ""
    n = re.search(r"(\d{3,4})\s*$", m.group(1).strip())
    return n.group(1) if n else ""


def consultar_estado_sunat(ruc: str, tipo: str, numeracion: str):
    """
    Pregunta a SUNAT si un comprobante está registrado.

    Devuelve (codigo, mensaje, cdr_zip) donde cdr_zip son los bytes del CDR cuando
    SUNAT lo entrega, o None. Ante cualquier fallo devuelve (None, motivo, None):
    quien llama debe tratar esa respuesta como "no sé", nunca como "no existe".
    Reenviar un comprobante que en realidad sí llegó lo duplica ante SUNAT.
    """
    if not (SOL_USUARIO and SOL_CLAVE):
        return None, "faltan SOL_USUARIO y SOL_CLAVE en el .env", None
    if "-" not in numeracion:
        return None, f"numeración sin serie: {numeracion!r}", None

    serie, correlativo = numeracion.split("-", 1)
    sobre = _SOBRE_CONSULTA.format(
        usuario=f"{ruc}{SOL_USUARIO}",
        clave=SOL_CLAVE,
        ruc=ruc,
        tipo=tipo,
        serie=serie,
        # SUNAT espera el correlativo como número, sin los ceros de la izquierda.
        numero=correlativo.lstrip("0") or "0",
    )
    peticion = urllib.request.Request(
        SUNAT_CONSULTA_URL,
        data=sobre.encode("utf-8"),
        # El SOAPAction no es opcional: sin él SUNAT despacha a getStatus —la consulta
        # de tickets— y responde "El ticket no existe" para cualquier comprobante.
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "urn:getStatusCdr"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=30) as r:
            respuesta = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        detalle = _texto_de_nodo(cuerpo, "faultstring") or f"HTTP {e.code}"
        logger.warning("Consulta a SUNAT rechazada para %s-%s: %s", tipo, numeracion, detalle)
        return None, detalle, None
    except Exception as e:
        logger.warning("No se pudo consultar a SUNAT por %s-%s: %s", tipo, numeracion, e)
        return None, str(e), None

    codigo  = _texto_de_nodo(respuesta, "statusCode")
    mensaje = _texto_de_nodo(respuesta, "statusMessage")
    b64     = _texto_de_nodo(respuesta, "content")
    cdr = None
    if b64:
        try:
            cdr = base64.b64decode(b64)
        except (ValueError, binascii.Error):
            logger.exception("SUNAT devolvió un CDR ilegible para %s-%s", tipo, numeracion)
    return codigo or None, mensaje, cdr


def _contar_consulta_fallida(tipo: str, numeracion: str, codigo: str, mensaje: str) -> int:
    """
    Suma una consulta sin respuesta útil y devuelve cuántas seguidas lleva.

    Va en reintentos.json, bajo su propia clave, por el mismo motivo que el resto del
    archivo: PM2 reinicia el daemon solo, y un contador en memoria volvería a cero en
    cada reinicio —justo cuando mas importa saber que esto lleva horas—. La clave
    incluye el tipo porque una consulta se hace por (tipo, numeracion), a diferencia
    del contador de reenvios, que se lleva solo por numeracion.
    """
    clave = f"consulta:{tipo}-{numeracion}"
    with _lock_reintentos:
        datos = _leer_reintentos()
        registro = datos.get(clave) or {}
        veces = int(registro.get("consultas", 0)) + 1
        datos[clave] = {
            "tipo": tipo,
            "consultas": veces,
            "ultimo": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "codigo": codigo,
            "motivo": mensaje or registro.get("motivo", ""),
        }
        _guardar_reintentos(datos)
        return veces


def _horas_en_proceso(numeracion: str) -> float:
    """
    Horas que lleva un ticket contestando "todavía lo estoy procesando".

    Se anota la primera vez y de ahí se mide. Va en reintentos.json y no en memoria
    por el mismo motivo que el resto del archivo: PM2 reinicia el daemon solo, y un
    contador en memoria arrancaría de cero en cada reinicio —justo cuando lo que hace
    falta saber es que esto lleva horas—.

    La cuenta arranca al primer "en proceso" y no cuando se genero el resumen: lo que
    interesa es hace cuanto que SUNAT viene diciendo lo mismo, no cuanto hace que
    existe el documento.
    """
    clave = f"proceso:{numeracion}"
    ahora = datetime.now()
    with _lock_reintentos:
        datos = _leer_reintentos()
        registro = datos.get(clave) or {}
        desde = registro.get("desde")
        if not desde:
            datos[clave] = {"desde": ahora.strftime("%Y-%m-%d %H:%M:%S")}
            _guardar_reintentos(datos)
            return 0.0
    try:
        return (ahora - datetime.strptime(desde, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600
    except ValueError:
        return 0.0


def _olvidar_en_proceso(numeracion: str):
    """El ticket dejó de estar en proceso: la cuenta de horas ya no importa."""
    clave = f"proceso:{numeracion}"
    with _lock_reintentos:
        datos = _leer_reintentos()
        if datos.pop(clave, None) is not None:
            _guardar_reintentos(datos)


def _olvidar_consulta_fallida(tipo: str, numeracion: str):
    """SUNAT respondió algo concluyente: la racha de consultas fallidas ya no importa."""
    clave = f"consulta:{tipo}-{numeracion}"
    with _lock_reintentos:
        datos = _leer_reintentos()
        if datos.pop(clave, None) is not None:
            _guardar_reintentos(datos)


def estado_en_sunat(ruc: str, tipo: str, numeracion: str) -> str:
    """
    'registrado' | 'no_registrado' | 'desconocido', más el CDR si SUNAT lo entrega.

    La distinción entre 'no_registrado' y 'desconocido' es lo importante: solo el
    primero autoriza a reenviar. Cualquier código que no esté en la lista blanca se
    trata como desconocido, porque reenviar algo que en realidad sí llegó lo duplica
    ante SUNAT, y eso no se deshace sin una nota de crédito.
    """
    codigo, mensaje, cdr = consultar_estado_sunat(ruc, tipo, numeracion)
    if codigo is None:
        return "desconocido", None, mensaje
    if cdr:
        _olvidar_consulta_fallida(tipo, numeracion)
        return "registrado", cdr, mensaje
    if codigo in _CODIGOS_NO_REGISTRADO:
        _olvidar_consulta_fallida(tipo, numeracion)
        return "no_registrado", None, mensaje

    # Un código de consulta fallida no dice nada del comprobante: dice que SUNAT no
    # pudo traer la constancia. Se vuelve a preguntar más tarde en vez de dar el
    # comprobante por perdido, pero se lleva la cuenta: si el servicio no se
    # recupera, alguien tiene que enterarse.
    fallidas = _contar_consulta_fallida(tipo, numeracion, codigo, mensaje)
    if codigo in _CODIGOS_CONSULTA_FALLIDA:
        if fallidas >= MAX_CONSULTAS_FALLIDAS:
            logger.error(
                "%s-%s lleva %d consultas seguidas sin respuesta útil de SUNAT "
                "(%s: %s). REQUIERE REVISIÓN MANUAL: verificar en el portal de SUNAT "
                "si el comprobante está aceptado.",
                tipo, numeracion, fallidas, codigo, mensaje,
            )
        else:
            logger.info(
                "SUNAT no pudo darnos la constancia de %s-%s (%s: %s); "
                "se vuelve a consultar más tarde (%d/%d).",
                tipo, numeracion, codigo, mensaje, fallidas, MAX_CONSULTAS_FALLIDAS,
            )
        return "desconocido", None, mensaje

    logger.warning(
        "SUNAT respondió por %s-%s un código que no sabemos interpretar (%s: %s); "
        "no se reenvía por las dudas.", tipo, numeracion, codigo, mensaje,
    )
    if fallidas >= MAX_CONSULTAS_FALLIDAS:
        logger.error(
            "%s-%s lleva %d consultas seguidas con el código %s. REQUIERE REVISIÓN "
            "MANUAL: el daemon no sabe interpretarlo y no va a resolverse solo.",
            tipo, numeracion, fallidas, codigo,
        )
    return "desconocido", None, mensaje


def _guardar_cdr(ruc: str, tipo: str, numeracion: str, cdr: bytes, mensaje: str):
    """
    Deja el CDR recuperado en RPTA, con el mismo nombre que le pondría el SFS.

    De ahí lo levanta el hilo CDR y lo procesa como cualquier otro. El nombre
    importa más de lo que parece: el XML de un CDR de consulta trae la numeración en
    otro formato que la de un envío normal, así que es el nombre —armado desde el
    NUM_DOCU canónico— el que permite reconciliarla (ver _reconciliar_numeracion).
    Se escribe con nombre temporal y se renombra para que watchdog no lo levante a
    medio escribir.

    Lo que este camino NO deja resuelto, a diferencia del normal, es la fila en la
    bandeja del SFS: sigue con el error de red que la trajo hasta acá. La cierra
    _cerrar_documento_en_sfs() una vez que el comprobante quedó cerrado en la BD de
    la aplicación, no antes.
    """
    os.makedirs(SFS_RPTA_DIR, exist_ok=True)
    destino = os.path.join(SFS_RPTA_DIR, f"R{ruc}-{tipo}-{numeracion}.zip")
    with open(destino + ".tmp", "wb") as fh:
        fh.write(cdr)
    os.replace(destino + ".tmp", destino)
    logger.info(
        "CDR de %s-%s recuperado desde SUNAT (%s); queda en RPTA para procesar.",
        tipo, numeracion, mensaje,
    )

# ---------------------------------------------------------------------------
# Consulta del ticket de un resumen diario
# ---------------------------------------------------------------------------

# Un resumen no devuelve su CDR en el acto como una factura: SUNAT responde un
# ticket y hay que volver a preguntar por él. El SFS sabe hacerlo, pero solo desde
# un job programado (ActualizarBajasJob) que exige tener el temporizador prendido,
# y prenderlo levantaría también sus jobs de generar/enviar, que harían por su
# cuenta lo mismo que este daemon hace por REST. Por eso la consulta la hace el
# daemon, con el mismo patrón que ya usa para recuperar CDR perdidos.
_SOBRE_TICKET = """<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:ser="http://service.sunat.gob.pe">
  <soapenv:Header>
    <wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
      <wsse:UsernameToken>
        <wsse:Username>{usuario}</wsse:Username>
        <wsse:Password>{clave}</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>
  </soapenv:Header>
  <soapenv:Body>
    <ser:getStatus>
      <ticket>{ticket}</ticket>
    </ser:getStatus>
  </soapenv:Body>
</soapenv:Envelope>"""

# Códigos de getStatus (distintos de los de getStatusCdr): 0 y 99 traen el CDR —el
# 99 es el de un resumen procesado CON errores, y su CDR explica cuáles—, mientras
# que el 98 significa que SUNAT todavía lo está procesando.
#
# Van SIN los ceros a la izquierda porque se comparan contra _norm_codigo_ticket().
# SUNAT no es consistente consigo mismo en este mismo servicio: verificado en
# producción el 2026-09-06, la aceptación vuelve como '0' —un carácter— y el "en
# proceso" como '0098' —cuatro—. Con las constantes escritas a mano, '0098' == '98'
# daba False siempre y la rama de "en proceso" era código muerto: la respuesta más
# común de SUNAT se contaba como consulta fallida y terminaba reportando REVISIÓN
# MANUAL sobre un resumen que estaba avanzando normalmente.
_TICKET_CON_CDR   = ("0", "98", "99")
_TICKET_EN_PROCESO = "98"
# Un ticket se consume al consultarlo: a la segunda vez SUNAT responde con este
# código y ya no hay CDR que recuperar por esa vía. Es el único veredicto definitivo
# de la consulta de tickets —todo lo demás merece otro intento— y por eso vale la
# pena distinguirlo en vez de tratarlo como una falla más.
_TICKET_NO_EXISTE = "127"


def _norm_codigo_ticket(codigo) -> str:
    """
    El código de getStatus sin los ceros de la izquierda, para poder compararlo.

    Existe porque SUNAT devuelve el mismo código en anchos distintos según la
    respuesta ('0' contra '0098'), y comparar el texto crudo hacía fallar la
    comparación justo en el caso más frecuente.

    El '0' se conserva como '0' y no se convierte en cadena vacía: vacío significa
    "SUNAT no dijo nada" —una falla de transporte— y cero significa "aceptado". Son
    dos cosas opuestas y aplastarlas daría por bueno un envío que nunca respondió.
    """
    texto = _texto(codigo)
    if not texto:
        return ""
    return texto.lstrip("0") or "0"


def _url_bill_service() -> str:
    """
    Endpoint de envío del SFS (RUTA_SERV_CDP de constantes.properties), que es el
    mismo servicio donde se consulta el ticket.

    Se lee de ahí en vez de tener su propia variable para que la consulta salga
    SIEMPRE al ambiente al que el SFS está enviando: si alguien pasa el SFS de beta
    a producción, esto lo sigue solo. Preguntarle a producción por un ticket de
    beta —o al revés— devolvería "el ticket no existe".
    """
    try:
        # utf-8-sig y no utf-8: si alguien edita el archivo con el Bloc de notas le
        # queda un BOM al inicio, y con utf-8 ese caracter invisible se pega al
        # nombre de la primera propiedad.
        with open(SFS_CONSTANTES_PATH, encoding="utf-8-sig", errors="replace") as fh:
            for linea in fh:
                linea = linea.strip()
                # Las variantes que no se usan quedan comentadas con '#', y hay una
                # por cada tipo de servicio y ambiente: solo vale la activa.
                if linea.startswith("RUTA_SERV_CDP="):
                    return linea.split("=", 1)[1].strip()
    except OSError:
        logger.exception(
            "No se pudo leer %s para ubicar el servicio de SUNAT.", SFS_CONSTANTES_PATH
        )
    return ""


def consultar_ticket_sunat(ruc: str, ticket: str):
    """
    Pregunta a SUNAT por el resultado de un ticket de resumen.

    Devuelve (codigo, mensaje, cdr_zip). Un None en el código significa "todavía no
    sé" —falla de transporte, credenciales, servicio caído— y quien llama debe
    reintentar más tarde.

    Cuando SUNAT rechaza con un fault codificado, ese código SÍ vuelve: es la única
    forma de distinguir un ticket que ya se consumió (0127, definitivo) de un
    "Internal Error" pasajero. Aplastar los dos en None hacía que un resumen trabado
    se reintentara para siempre o no se reintentara nunca, según de qué lado se
    errara.
    """
    if not (SOL_USUARIO and SOL_CLAVE):
        return None, "faltan SOL_USUARIO y SOL_CLAVE en el .env", None
    url = _url_bill_service()
    if not url:
        return None, "no se pudo determinar el servicio de SUNAT (RUTA_SERV_CDP)", None

    sobre = _SOBRE_TICKET.format(usuario=f"{ruc}{SOL_USUARIO}", clave=SOL_CLAVE, ticket=ticket)
    peticion = urllib.request.Request(
        url,
        data=sobre.encode("utf-8"),
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": "urn:getStatus"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=30) as r:
            respuesta = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")
        detalle = _texto_de_nodo(cuerpo, "faultstring") or f"HTTP {e.code}"
        codigo_fault = _codigo_de_fault(cuerpo)
        logger.warning("Consulta del ticket %s rechazada por SUNAT: %s", ticket, detalle)
        return codigo_fault or None, detalle, None
    except Exception as e:
        logger.warning("No se pudo consultar el ticket %s: %s", ticket, e)
        return None, str(e), None

    codigo  = _texto_de_nodo(respuesta, "statusCode")
    mensaje = _texto_de_nodo(respuesta, "statusMessage")
    b64     = _texto_de_nodo(respuesta, "content")
    cdr = None
    if b64:
        try:
            cdr = base64.b64decode(b64)
        except (ValueError, binascii.Error):
            logger.exception("SUNAT devolvió un CDR ilegible para el ticket %s", ticket)
    return codigo or None, mensaje, cdr


def _resumenes_con_ticket(ruc_emisor: str) -> list:
    """
    [(num_docu, ticket)] de los resúmenes que todavía pueden resolverse por su ticket.

    Se pide que el resumen NO esté cerrado y que conserve ticket, en vez de exigir
    los estados '08'/'09' como antes. El motivo: si la consulta del ticket falla
    —SUNAT devolviendo "Internal Error", por ejemplo— el SFS deja el resumen en '05',
    y con el filtro viejo eso lo sacaba de esta lista para siempre. El ticket seguía
    guardado y seguía siendo válido, pero nadie volvía a usarlo.

    Eso paso en produccion el 2026-09-06: siete resumenes quedaron en '05' por una
    falla pasajera de SUNAT y retuvieron 1239 boletas durante 12 horas, cuando los
    siete tickets respondian "aceptado" al consultarlos a mano.

    Un ticket ya consumido tambien entra acá, y esta bien: SUNAT contesta 0127 y de
    eso se encarga recuperar_cdr_resumenes(), que lo distingue de una consulta que
    fallo y merece otro intento.
    """
    if not os.path.exists(SFS_BD_PATH):
        return []
    marcas = _marcas(len(_ESTADOS_RESUMEN_ABIERTO))
    try:
        with _sfs_bd() as sfs:
            return [
                (_texto(num), _texto(tk))
                for num, tk in sfs.execute(
                    f"SELECT NUM_DOCU, NUM_TICKET FROM DOCUMENTO "
                    f"WHERE NUM_RUC=? AND TIP_DOCU=? AND IND_SITU IN ({marcas}) "
                    f"AND NUM_TICKET IS NOT NULL AND NUM_TICKET <> ''",
                    (ruc_emisor, _TIPO_RC, *_ESTADOS_RESUMEN_ABIERTO),
                )
            ]
    except sqlite3.Error:
        logger.exception("No se pudo leer la BD del SFS para buscar tickets de resumen.")
        return []


def _ticket_de_resumen(ruc_emisor: str, numeracion: str) -> str:
    """
    Ticket guardado de un resumen, o "" si no tiene.

    Sirve como evidencia de si SUNAT llegó a recibirlo: el ticket lo escribe el SFS
    con lo que devuelve sendSummary, así que sin ticket el envío no llegó. Es lo que
    permite decidir si un resumen trabado se puede volver a armar sin arriesgar
    declarar las mismas boletas dos veces.
    """
    if not os.path.exists(SFS_BD_PATH):
        return ""
    try:
        with _sfs_bd() as sfs:
            fila = sfs.execute(
                "SELECT NUM_TICKET FROM DOCUMENTO "
                "WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                (ruc_emisor, _TIPO_RC, numeracion),
            ).fetchone()
    except sqlite3.Error:
        # Ante la duda se responde "tiene ticket": eso frena el reenvío, que es el
        # lado seguro. Decir que no tiene habilitaría a declarar de nuevo algo que
        # quizá SUNAT ya recibió.
        logger.exception("No se pudo leer el ticket del resumen %s.", numeracion)
        return "desconocido"
    return _texto(fila[0]) if fila else ""


def _veredicto_cdr(parsed: dict) -> str:
    """
    Lo que SUNAT contestó, en una línea, para DES_OBSE de la bandeja del SFS.

    Ese campo es lo primero que mira una persona cuando algo sale mal, así que tiene
    que decir la verdad. Hasta el 2026-09-09 se escribía un "Aceptado (CDR procesado)"
    fijo, también cuando el CDR era un rechazo: quedaron 40 resúmenes rotulados
    "Aceptado" de los cuales 39 SUNAT los había rechazado —34 con el código 2282,
    "Existe documento ya informado anteriormente"—.

    No hubo daño funcional: sus boletas siguieron en enviado=0, que es lo correcto. El
    daño fue de diagnóstico. Ese texto llevó a concluir que había 143 boletas
    declaradas 40 veces y que hacía falta una comunicación de baja ante SUNAT, cuando
    en realidad SUNAT había rechazado los repetidos y no había ningún duplicado. La
    conclusión correcta recién apareció al abrir los CDR archivados a mano.
    """
    estado = _texto(parsed.get("status")) or "PROCESADO"
    texto = estado.capitalize()
    codigo = _texto(parsed.get("codigo"))
    if codigo:
        texto += f" — código {codigo}"
    descripcion = _texto(parsed.get("descripcion"))
    if descripcion:
        texto += f": {descripcion}"
    return texto[:_MAX_DES_OBSE]


def _veredicto_archivado(ruc: str, tip: str, num: str) -> str:
    """
    Veredicto leído del CDR que ya está en disco, para cerrar sin tener que afirmarlo.

    Hace falta donde se cierra un documento por el solo hecho de que su CDR existe
    —ver _activar_pendientes_sfs_bd() y el rescate de recuperar_cdr_resumenes()—: ahí
    no hay un `parsed` a mano, y suponer "aceptado" es exactamente lo que hacía mentir
    a la bandeja. Si el archivo no se puede leer, el texto lo dice en vez de inventar
    un veredicto.
    """
    for carpeta in (DIR_PROCESADOS, SFS_RPTA_DIR):
        ruta = os.path.join(carpeta, f"R{ruc}-{tip}-{num}.zip")
        try:
            with zipfile.ZipFile(ruta) as z:
                xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
                if xmls:
                    return _veredicto_cdr(parsear_xml_cdr(z.read(xmls[0])))
        except (OSError, zipfile.BadZipFile, ET.ParseError):
            continue
    return "CDR procesado; ver el CDR archivado"


def _cerrar_resumen_en_sfs(ruc_emisor: str, numeracion: str, veredicto: str = ""):
    """
    Da por cerrado el resumen en la bandeja del SFS una vez que su CDR está en RPTA.

    Un ticket de SUNAT se consume al consultarlo: si el SFS lo vuelve a consultar
    después de que el daemon ya lo usó, recibe "El ticket no existe" y deja el
    resumen en IND_SITU='05'. Ese estado cuenta como bloqueado, así que el resumen
    se reportaría como trabado en cada ciclo y sus archivos nunca saldrían de DATA
    —pese a estar perfectamente emitido y con las boletas ya cerradas—.

    Se marca '03' con el mismo criterio que usa _activar_pendientes_sfs_bd() cuando
    encuentra un CDR ya descargado: en la bandeja del SFS ese estado significa "ya
    no me ocupo de esto". El veredicto real de SUNAT no vive acá sino en el CDR, que
    es quien decide si las boletas quedan en enviado=true o con su motivo de rechazo.

    El WHERE sale de _ESTADOS_RESUMEN_ABIERTO, la misma constante que decide a cuáles
    consultarles el ticket. Antes exigía '08'/'09' escrito a mano y quedó atrás
    cuando la consulta se amplió para rescatar los resúmenes en '05': el rescate
    funcionaba, pero el cierre no encontraba la fila, el UPDATE afectaba cero filas y
    el resumen se quedaba en '05' para siempre —reconsultándose y reportándose como
    trabado aunque sus boletas ya estuvieran cerradas.
    """
    if not os.path.exists(SFS_BD_PATH):
        return
    # El '03' es el mismo para un aceptado y para un rechazado —significa "ya no me
    # ocupo de esto"—, así que el único lugar donde se puede leer qué contestó SUNAT
    # es este texto. Quien llama pasa el veredicto que ya tiene; si no lo tiene, se lo
    # lee del CDR archivado en vez de suponerlo.
    obse = veredicto or _veredicto_archivado(ruc_emisor, _TIPO_RC, numeracion)
    try:
        with _sfs_bd(escritura=True) as sfs:
            sfs.execute(
                f"UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE=? "
                f"WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                f"AND IND_SITU IN ({_marcas(len(_ESTADOS_RESUMEN_ABIERTO))})",
                (obse[:_MAX_DES_OBSE], ruc_emisor, _TIPO_RC, numeracion,
                 *_ESTADOS_RESUMEN_ABIERTO),
            )
    except sqlite3.Error:
        logger.exception("No se pudo cerrar el resumen %s en la bandeja del SFS.", numeracion)


def recuperar_cdr_resumenes(ruc_emisor: str):
    """
    Consulta el ticket de cada resumen enviado y baja su CDR cuando ya está listo.

    El CDR queda en RPTA y de ahí en adelante el flujo es el de siempre: el hilo
    CDR lo levanta y _actualizar_sql_cdr() lo reparte entre todas las boletas que
    el resumen agrupa.
    """
    ahora = time.monotonic()
    for numeracion, ticket in _resumenes_con_ticket(ruc_emisor):
        if _tiene_cdr(ruc_emisor, _TIPO_RC, numeracion):
            # Con el CDR ya archivado, el resumen deberia estar cerrado en la bandeja
            # —lo cierra _actualizar_sql_cdr() al procesarlo—, pero si por lo que sea
            # no lo esta, nada volveria a moverlo: este continue corta antes de
            # reconsultar, el CDR ya no vuelve a RPTA y el hilo CDR no lo reprocesa,
            # asi que _cerrar_resumen_en_sfs() no llega a correr nunca. El resumen se
            # quedaba en su estado abierto de forma permanente, reconsultandose no
            # —eso lo frena este mismo corte— pero si reportandose como trabado en
            # cada ciclo, diciendo que retiene boletas que ya estan cerradas.
            #
            # Solo con el CDR en procesados/, no en RPTA: que el archivo exista no
            # significa que el hilo CDR ya lo haya repartido entre las boletas, y
            # cerrar antes daria el resumen por bueno con sus boletas todavia en
            # enviado=0.
            if _cdr_ya_procesado(ruc_emisor, _TIPO_RC, numeracion):
                _cerrar_resumen_en_sfs(ruc_emisor, numeracion)
            continue
        previo = _ultima_consulta.get((_TIPO_RC, numeracion))
        if previo is not None and ahora - previo < _COOLDOWN_CONSULTA_SEG:
            continue
        _ultima_consulta[(_TIPO_RC, numeracion)] = ahora

        codigo, mensaje, cdr = consultar_ticket_sunat(ruc_emisor, ticket)
        codigo = _norm_codigo_ticket(codigo)
        if codigo == _TICKET_NO_EXISTE:
            # Definitivo: el ticket se consumió y ya no hay nada que preguntarle a
            # SUNAT. No se reintenta —daría siempre lo mismo— y se reporta, porque
            # sus boletas siguen retenidas y solo una persona puede decidir qué
            # hacer con ellas (ver _reportar_resumenes_trabados).
            logger.error(
                "El ticket %s del resumen %s ya no existe en SUNAT (%s). Sus boletas "
                "siguen retenidas: hay que verificar en el portal si el resumen fue "
                "aceptado antes de tocar nada.",
                ticket, numeracion, mensaje,
            )
            continue
        # La racha se olvida solo ante una respuesta concluyente: el CDR recuperado o
        # el "todavía lo estoy procesando". Antes se olvidaba apenas el código no
        # fuera None, con lo que un fault con código —el 0100, por ejemplo, que es un
        # transitorio documentado de SUNAT— reseteaba la cuenta en cada intento y
        # jamás llegaba al tope: el resumen se reconsultaba para siempre sin que nadie
        # se enterara, que es justo lo que MAX_CONSULTAS_FALLIDAS venía a evitar.
        if codigo == _TICKET_EN_PROCESO:
            # "Todavía lo estoy procesando" no es una falla, así que no gasta el
            # presupuesto de consultas fallidas. Pero tampoco puede repetirse en un
            # INFO tranquilo para siempre: un ticket que dice esto durante 24 horas
            # está muerto del lado de SUNAT, no encolado —verificado el 2026-09-06,
            # cuando otro resumen enviado ese mismo día se proceso en minutos—.
            # Por eso se lleva desde cuándo, y pasado el umbral el aviso escala.
            _olvidar_consulta_fallida(_TIPO_RC, numeracion)
            horas = _horas_en_proceso(numeracion)
            if horas >= HORAS_TICKET_EN_PROCESO:
                logger.error(
                    "El ticket %s del resumen %s lleva %.1f h en 'en proceso' y "
                    "retiene %d boleta(s). REQUIERE REVISIÓN MANUAL: verificar en el "
                    "portal de SUNAT si el resumen se declaró. NO se reenvía solo: ya "
                    "tiene ticket, así que SUNAT lo recibió y reenviarlo declararía "
                    "las mismas boletas dos veces.",
                    ticket, numeracion, horas, len(_boletas_de_resumen(numeracion)),
                )
            else:
                logger.info("SUNAT todavía procesa el resumen %s (ticket %s, %.1f h).",
                            numeracion, ticket, horas)
            continue
        if not (cdr and codigo in _TICKET_CON_CDR):
            # Sin CDR no hay veredicto, venga o no con código: cuenta contra el tope.
            veces = _contar_consulta_fallida(
                _TIPO_RC, numeracion, _texto(codigo) or "sin codigo", mensaje)
            if veces >= MAX_CONSULTAS_FALLIDAS:
                logger.error(
                    "El ticket %s del resumen %s lleva %d consultas sin respuesta útil "
                    "(%s: %s). REQUIERE REVISIÓN MANUAL: sus boletas siguen retenidas.",
                    ticket, numeracion, veces, _texto(codigo) or "sin código", mensaje,
                )
            else:
                logger.info(
                    "Ticket %s de %s: sin respuesta útil (%s: %s); se reintenta (%d/%d).",
                    ticket, numeracion, _texto(codigo) or "sin código", mensaje,
                    veces, MAX_CONSULTAS_FALLIDAS,
                )
            continue
        _olvidar_consulta_fallida(_TIPO_RC, numeracion)
        # Llegó el CDR: si venía de una racha de "en proceso", esa cuenta ya no importa.
        _olvidar_en_proceso(numeracion)
        # Vale tanto para el aceptado como para el rechazado: el parser del CDR
        # decide cuál es, igual que con cualquier otro comprobante.
        # El resumen NO se cierra acá: recién cuando el hilo CDR termine de
        # procesarlo. Cerrarlo al bajarlo dejaba un hueco de segundos en el que
        # el resumen ya figuraba cerrado —y por lo tanto sus boletas libres—
        # pero todavía no estaban en enviado=true, así que el ciclo siguiente
        # las tomaba y armaba otro resumen con las mismas.
        _guardar_cdr(ruc_emisor, _TIPO_RC, numeracion, cdr, f"ticket {ticket}: {mensaje}")

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
    """
    True si el CDR de este comprobante ya está en disco.

    Se exige que el archivo tenga contenido, no solo que exista: un ZIP que quedó en
    0 bytes hacía que recuperar_cdr_pendientes() diera el CDR por recuperado y no
    volviera a consultarle a SUNAT, mientras el barrido tampoco podía procesarlo. El
    comprobante quedaba en enviado=0 sin ninguna via de salida (ver
    _archivo_abandonado).
    """
    nombre = f"R{ruc}-{tip}-{num}.zip"
    for d in (SFS_RPTA_DIR, DIR_PROCESADOS):
        ruta = os.path.join(d, nombre)
        try:
            if os.path.getsize(ruta) > 0:
                return True
        except OSError:
            continue
    return False


def _cdr_ya_procesado(ruc: str, tip: str, num: str) -> bool:
    """
    True si el CDR ya paso por el hilo CDR y quedo archivado.

    La distincion con _tiene_cdr() importa: que el archivo este en RPTA solo dice
    que se bajo, no que se haya repartido entre las boletas. Recien cuando el
    barrido lo procesa sin errores lo mueve a procesados/, y esa mudanza es la
    unica evidencia de que el CDR ya hizo su trabajo.
    """
    ruta = os.path.join(DIR_PROCESADOS, f"R{ruc}-{tip}-{num}.zip")
    try:
        return os.path.getsize(ruta) > 0
    except OSError:
        return False


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
        if tip == _TIPO_RC:
            # El nombre de archivo de un resumen va sin el "RC-" del id (lo exige
            # validarNombreArchivo del SFS), pero en la bandeja el número sí lo
            # lleva. Sin reponerlo acá, sus archivos nunca calzaban y quedaban en
            # DATA para siempre. Ver _nombre_archivo_rc().
            num = f"{_TIPO_RC}-{num}"
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
                # Se cierra porque el CDR existe, no porque diga que fue aceptado: hay
                # que leerlo para no rotular "Aceptado" algo que SUNAT rechazó.
                sfs.execute(
                    "UPDATE DOCUMENTO SET IND_SITU='03', DES_OBSE=? "
                    "WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? AND IND_SITU IN ('01','02')",
                    (_veredicto_archivado(ruc_emisor, tip, num)[:_MAX_DES_OBSE],
                     ruc_emisor, tip, num),
                )
                _eliminar_data_files(nom_arch or f"{ruc_emisor}-{tip}-{num}")
                continue
            docs_extra.append({"num_ruc": ruc_emisor, "tip_docu": tip, "num_docu": num})
    if docs_extra:
        logger.info("%d doc(s) en SFS BD pendientes de activar.", len(docs_extra))
        activar_procesamiento_sfs(docs_extra)


def _docs_enviados_sin_cdr(ruc_emisor: str) -> list:
    """
    Documentos que el SFS dice haber enviado y que siguen sin cerrarse, con cuántos
    minutos llevan así. Son los únicos candidatos a consultarle a SUNAT: si no tienen
    fecha de envío es que nunca salieron, y preguntar por ellos no tiene sentido.

    Los resúmenes (RC) quedan afuera: su respuesta vive detrás de un ticket y se
    consulta con getStatus, no con getStatusCdr (ver recuperar_cdr_resumenes). Si
    entraran acá, se les preguntaría con una serie-número que no existe como tal, y
    una respuesta de "no registrado" borraría el resumen de la bandeja junto con su
    ticket — perdiendo el único modo de recuperar su CDR.
    """
    if not os.path.exists(SFS_BD_PATH):
        return []
    marcas = _marcas(len(_ESTADOS_CERRADOS))
    ahora = datetime.now()
    pendientes = []
    try:
        with _sfs_bd() as sfs:
            filas = sfs.execute(
                "SELECT TIP_DOCU, NUM_DOCU, FEC_ENVI FROM DOCUMENTO "
                f"WHERE NUM_RUC=? AND TIP_DOCU<>? AND IND_SITU NOT IN ({marcas}) "
                "AND FEC_ENVI IS NOT NULL AND FEC_ENVI <> ''",
                (ruc_emisor, _TIPO_RC, *_ESTADOS_CERRADOS),
            ).fetchall()
    except sqlite3.Error:
        logger.exception("No se pudo leer la BD del SFS para buscar documentos sin CDR.")
        return []

    for tip, num, fec_envi in filas:
        try:
            enviado_el = datetime.strptime(_texto(fec_envi), "%d/%m/%Y %H:%M:%S")
        except ValueError:
            continue
        pendientes.append((_texto(tip), _texto(num), (ahora - enviado_el).total_seconds() / 60))
    return pendientes


def recuperar_cdr_pendientes(ruc_emisor: str):
    """
    Para cada comprobante enviado que lleva rato sin CDR, le pregunta a SUNAT.

    Es la salida al caso de la conexión cortada: el SFS mandó el documento pero la
    respuesta nunca volvió, así que nadie sabe si llegó. Si SUNAT lo tiene, su CDR
    queda en RPTA y el hilo CDR lo cierra solo. Si confirma que no lo tiene, se
    borra de la bandeja del SFS para que el próximo ciclo lo regenere y reenvíe.
    """
    ahora = time.monotonic()
    for tip, num, minutos in _docs_enviados_sin_cdr(ruc_emisor):
        if minutos < CONSULTA_SUNAT_TRAS_MIN:
            continue
        if _tiene_cdr(ruc_emisor, tip, num):
            continue  # el CDR ya está en disco, lo levanta el hilo CDR
        previo = _ultima_consulta.get((tip, num))
        if previo is not None and ahora - previo < _COOLDOWN_CONSULTA_SEG:
            continue
        _ultima_consulta[(tip, num)] = ahora

        logger.info(
            "%s-%s lleva %.0f min enviado sin CDR; consultando a SUNAT...", tip, num, minutos
        )
        estado, cdr, mensaje = estado_en_sunat(ruc_emisor, tip, num)
        if estado == "registrado":
            _guardar_cdr(ruc_emisor, tip, num, cdr, mensaje)
        elif estado == "no_registrado":
            # No llegó: se saca de la bandeja para que vuelva a generarse y salir.
            logger.warning(
                "SUNAT no tiene %s-%s: el envío no llegó. Vuelve a la cola.", tip, num
            )
            with _sfs_bd(escritura=True) as sfs:
                sfs.execute(
                    "DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=?",
                    (ruc_emisor, tip, num),
                )
            _eliminar_data_files(f"{ruc_emisor}-{tip}-{num}")
        else:
            logger.warning(
                "No se pudo determinar si SUNAT tiene %s-%s (%s); no se reenvía.",
                tip, num, mensaje,
            )


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


def _leer_resumenes() -> dict:
    try:
        with open(_RESUMENES_PATH, encoding="utf-8") as fh:
            datos = json.load(fh)
        return datos if isinstance(datos, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.exception("No se pudo leer %s; se reinicia el registro de resúmenes.", _RESUMENES_PATH)
        return {}


def _guardar_resumenes(datos: dict):
    try:
        escribir_archivo(_RESUMENES_PATH, json.dumps(datos, ensure_ascii=False, indent=2))
    except OSError:
        logger.exception("No se pudo guardar %s; el registro de resúmenes no persiste.", _RESUMENES_PATH)


def _siguiente_numeracion_rc(fecha: str) -> str:
    """
    RC-{fecha}-{NNN} (fecha=YYYYMMDD), con NNN correlativo propio del daemon — la
    única numeración que el daemon asigna en vez de leer: para todo lo demás la
    aplicación ya la puso antes de que el comprobante llegue acá.

    Con el prefijo "RC-" incluido, porque es la forma canónica del id: es la que
    usa SUNAT en el XML, la que el SFS guarda en DOCUMENTO.NUM_DOCU y espera en su
    API REST, y la que vuelve en el CDR. La ÚNICA excepción es el nombre de archivo
    en DATA, que se arma sin el prefijo (ver _nombre_archivo_rc).

    El correlativo no sale solo del archivo: se saltean los números que ya tengan
    un CDR en disco. Si resumenes.json se pierde o se borra a mano, el contador
    vuelve a 001 — y un CDR anterior con esa misma numeración haría que el daemon
    diera por contestado un resumen que nunca envió, cerrándolo sin que llegue a
    SUNAT. Pasó de verdad al reiniciar el contador.
    """
    with _lock_resumenes:
        datos = _leer_resumenes()
        n = int(datos.get("ultimo_correlativo", 0))
        while True:
            n += 1
            numeracion = f"{_TIPO_RC}-{fecha}-{n:03d}"
            if not _tiene_cdr(EMISOR_RUC_OVERRIDE, _TIPO_RC, numeracion):
                break
            logger.warning(
                "Ya existe un CDR para %s; se saltea ese número. El contador de "
                "resúmenes venía atrasado respecto de lo ya emitido.", numeracion,
            )
        datos["ultimo_correlativo"] = n
        _guardar_resumenes(datos)
    return numeracion


def _nombre_archivo_rc(ruc_emisor: str, numeracion_rc: str) -> str:
    """
    Nombre base del archivo en DATA para un resumen, sin el prefijo "RC-" del id.

    validarNombreArchivo() del SFS exige exactamente 4 tramos separados por guión
    (RUC-TIPO-SERIE-NUMERO). Como _nombre_base() ya agrega el tipo, dejar el "RC-"
    del id daría 5 tramos y el SFS descarta el archivo en silencio: no genera el
    XML ni lo registra en su bandeja, sin ningún error que lo delate (confirmado
    decompilando esa validación).
    """
    return _nombre_base(ruc_emisor, _TIPO_RC, _sin_prefijo_rc(numeracion_rc))


def _sin_prefijo_rc(numeracion_rc: str) -> str:
    prefijo = f"{_TIPO_RC}-"
    return numeracion_rc[len(prefijo):] if numeracion_rc.startswith(prefijo) else numeracion_rc


def _registrar_resumen(numeracion_rc: str, boletas: list):
    with _lock_resumenes:
        datos = _leer_resumenes()
        datos.setdefault("resumenes", {})[numeracion_rc] = {
            "boletas": boletas,
            "generado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        _guardar_resumenes(datos)


def _olvidar_resumen(numeracion_rc: str) -> list:
    """
    Libera las boletas de un resumen que no llegó a SUNAT y devuelve cuáles eran,
    CONSERVANDO el registro de qué llevaba.

    Corresponde cuando el envío falló sin devolver ticket, porque libera sus boletas
    para que se reagrupen en un resumen nuevo. Sin esto, reencolar un resumen no
    alcanzaba: al borrar su fila de la bandeja del SFS quedaba sin rastro, y
    _boletas_en_resumenes_activos() retiene ante la falta de rastro —correctamente,
    porque ahí no sabe qué paso—. Las boletas quedaban retenidas por un resumen que ya
    no existía: un bloqueo cambiado por otro.

    Hasta el 2026-09-09 esto hacía un pop() de la entrada, y eso son dos cosas
    distintas pegadas en una: LIBERAR las boletas —correcto— y OLVIDAR cuáles eran
    —nunca correcto—. La inferencia "sin ticket ⇒ SUNAT no lo recibió" no siempre
    vale: durante un bloqueo de ~19 horas varios envíos sí habían llegado y quedaron
    encolados del lado de SUNAT, que los aceptó al recuperarse. Ese CDR tardío llegaba
    a _actualizar_sql_cdr() y se encontraba sin mapeo, así que no podía cerrar nada;
    las boletas seguían en enviado=0, el ciclo las reagrupaba, y arrancaba el bucle de
    redeclaración que dejó 143 boletas declaradas 40 veces.

    Por eso la entrada se marca como descartada en vez de borrarse: es el único dato
    que permite honrar un CDR que llegue después. La poda de _podar_resumenes() se
    encarga de que el archivo no crezca sin fin.
    """
    with _lock_resumenes:
        datos = _leer_resumenes()
        entrada = (datos.get("resumenes") or {}).get(numeracion_rc)
        if entrada is None:
            return []
        entrada["descartado"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _guardar_resumenes(datos)
    return entrada.get("boletas", [])


def _resumen_descartado(numeracion_rc: str) -> str:
    """Cuándo se descartó este resumen, o "" si sigue vigente."""
    entrada = _leer_resumenes().get("resumenes", {}).get(numeracion_rc) or {}
    return _texto(entrada.get("descartado"))


def _marcar_resumen_cerrado(numeracion_rc: str, boletas: list = None):
    """
    Deja constancia de que este resumen ya cerró sus boletas.

    Es la única evidencia positiva de que se resolvió: la fila del SFS se limpia con
    el tiempo, y sin esto no habría forma de distinguir un resumen terminado de uno
    que quedó a medias. La poda lo necesita para no borrar entradas que todavía
    pueden hacer falta, y _boletas_en_resumenes_activos() para no avisar de un
    resumen "sin rastro" que en realidad terminó bien.
    """
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock_resumenes:
        datos = _leer_resumenes()
        resumenes = datos.setdefault("resumenes", {})
        # Puede no existir si el mapeo se reconstruyó desde FIRMA/: se crea igual, para
        # que quede el registro de qué boletas se cerraron y con qué resumen.
        entrada = resumenes.setdefault(numeracion_rc, {"boletas": list(boletas or []),
                                                       "generado": ahora})
        entrada["cerrado"] = ahora
        entrada.pop("descartado", None)   # llegó su CDR: ya no está descartado
        _guardar_resumenes(datos)


def _resumenes_que_repiten(numeracion_rc: str, boletas: list) -> list:
    """
    Otros resúmenes vigentes que declaran alguna de estas mismas boletas.

    Sirve para reconocer un duplicado ante SUNAT sin consultarle nada: si el CDR de un
    resumen descartado llega aceptado y sus boletas ya viajaron en otro resumen, esas
    boletas están declaradas dos veces y alguien va a tener que dar una de baja.
    """
    propias = set(boletas)
    repiten = []
    for otro, entrada in _leer_resumenes().get("resumenes", {}).items():
        if otro == numeracion_rc or entrada.get("descartado"):
            continue
        if propias.intersection(entrada.get("boletas", [])):
            repiten.append(otro)
    return sorted(repiten)


def _boletas_desde_firma(ruc_emisor: str, numeracion_rc: str) -> list:
    """
    Reconstruye qué boletas llevaba un resumen leyendo su XML firmado en FIRMA/.

    Último recurso para cuando la entrada de resumenes.json ya no está: el XML es lo
    que se le declaró a SUNAT, así que su lista de <cbc:ID> es la fuente autoritativa.
    Así se recuperaron a mano las 143 boletas del incidente del 2026-09-09, y así se
    pueden cerrar los CDR tardíos de los resúmenes que se descartaron ANTES de este
    arreglo —esas entradas ya se borraron y no hay forma de recuperarlas de otro lado—.

    El <cbc:ID> del propio resumen (RC-YYYYMMDD-NNN) no entra: el patrón exige los 4
    caracteres de una serie SUNAT, y el del resumen tiene solo 2 letras antes del guión.
    """
    ruta = os.path.join(_SFS_FIRMA_DIR, f"{_nombre_archivo_rc(ruc_emisor, numeracion_rc)}.xml")
    try:
        root = ET.parse(ruta).getroot()
    except (OSError, ET.ParseError):
        return []
    boletas = []
    for elem in root.iter():
        if not isinstance(elem.tag, str) or elem.tag.split("}")[-1].lower() != "id":
            continue
        texto = _texto(elem.text)
        if re.fullmatch(r"[A-Z][A-Z0-9]{3}-\d+", texto):
            boletas.append(texto)
    return list(dict.fromkeys(boletas))   # sin duplicados y en el orden del XML


def _motivo_para_frenar(boletas: list, fecha: str) -> str:
    """
    Por qué NO se debería armar otro resumen ahora mismo, o "" si se puede.

    El 2026-09-09 el ciclo armó, mandó y descartó 84 resúmenes en un día sin que nada
    lo notara: cada vuelta declaraba otra vez las mismas 143 boletas, y ninguna alarma
    distinguía eso de la operación normal. Frenar y pedir intervención cuesta una
    demora; seguir girando cuesta una comunicación de baja por cada boleta duplicada.

    Se mira cuántas veces se declaró cada boleta candidata y cuántos resúmenes lleva el
    día. Lo primero ataja el lote concreto que está girando en falso —es la señal más
    directa—; lo segundo es la red de seguridad por si el bucle vuelve de otra forma.
    """
    veces = {}
    buscadas = set(boletas)
    for entrada in _leer_resumenes().get("resumenes", {}).values():
        for b in buscadas.intersection(entrada.get("boletas", [])):
            veces[b] = veces.get(b, 0) + 1

    repetidas = sorted(b for b, v in veces.items() if v >= MAX_DECLARACIONES_BOLETA)
    if repetidas:
        muestra = ", ".join(repetidas[:_MAX_BLOQUEADOS_LOG])
        if len(repetidas) > _MAX_BLOQUEADOS_LOG:
            muestra += f" ... y {len(repetidas) - _MAX_BLOQUEADOS_LOG} mas"
        return (
            f"{len(repetidas)} boleta(s) ya se declararon {MAX_DECLARACIONES_BOLETA} "
            f"veces o mas sin cerrarse: {muestra}"
        )

    prefijo = f"{_TIPO_RC}-{fecha}-"
    del_dia = sum(1 for n in _leer_resumenes().get("resumenes", {}) if n.startswith(prefijo))
    if del_dia >= MAX_RESUMENES_DIA:
        return f"ya se generaron {del_dia} resúmenes hoy (tope {MAX_RESUMENES_DIA})"
    return ""


def _numeraciones_pendientes(conn, numeraciones) -> set:
    """Cuáles de estas numeraciones siguen en enviado=0."""
    buscadas = set(numeraciones)
    return {n for f in _bd().pendientes(conn)
            if (n := _texto(f.get("numeracion_comprobante"))) in buscadas}


def _resumen_vencido(numeracion_rc: str, entrada: dict, ahora: datetime) -> bool:
    """
    True si esta entrada ya cumplió su función y superó el margen de retención.

    Una entrada hace falta mientras su resumen pueda todavía resolverse: hasta que su
    CDR llegue y cierre sus boletas, más un margen holgado por si llega tarde —que es
    justamente el caso que _olvidar_resumen() viene a cubrir—.

    Un resumen SIN resolver no se poda por viejo que sea: ahí la antigüedad es
    exactamente la señal de que algo quedó trabado, y borrarlo perdería el único
    registro de qué boletas retiene.
    """
    try:
        generado = datetime.strptime(_texto(entrada.get("generado")), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False   # sin fecha legible no se toca nada
    if (ahora - generado).days < DIAS_RETENCION_RESUMENES:
        return False
    if entrada.get("cerrado") or entrada.get("descartado"):
        return True
    # Sin marca propia, el CDR en disco alcanza como prueba de que se resolvió: cubre
    # las entradas anteriores a que existieran esas marcas.
    return _tiene_cdr(EMISOR_RUC_OVERRIDE, _TIPO_RC, numeracion_rc)


def _podar_resumenes(conn):
    """
    Saca de resumenes.json las entradas ya resueltas que pasaron el margen.

    Hasta el 2026-09-09 nada podaba ese archivo: la única eliminación era el pop() de
    _olvidar_resumen(), que es justamente lo que este arreglo dejó de hacer. Sin una
    política de retención el archivo solo crece —199 KB y 8.432 referencias a boletas
    al momento del incidente, con entradas de 11 días atrás que ya no cumplían ninguna
    función—.

    Corre una vez por día porque es mantenimiento, no parte del flujo: así la consulta
    de pendientes que necesita no se repite en cada ciclo.

    Dos entradas nunca se podan, por viejas que sean: una cuyo resumen siga sin
    resolverse (ver _resumen_vencido) y una cuyas boletas sigan en enviado=0 aunque el
    resumen figure cerrado —pasa cuando SUNAT observó alguna línea y esa boleta quedó
    afuera, y su mapeo es lo que permite entender por qué—.
    """
    ahora = datetime.now()
    datos = _leer_resumenes()
    try:
        ultima = datetime.strptime(_texto(datos.get("ultima_poda")), "%Y-%m-%d %H:%M:%S")
        if (ahora - ultima).total_seconds() < 24 * 3600:
            return
    except ValueError:
        pass   # nunca se podó, o la marca está ilegible: se poda ahora

    vencidas = {n: e for n, e in (datos.get("resumenes") or {}).items()
                if _resumen_vencido(n, e, ahora)}
    pendientes = _numeraciones_pendientes(
        conn, {b for e in vencidas.values() for b in e.get("boletas", [])},
    ) if vencidas else set()

    podadas, retenidas = [], []
    with _lock_resumenes:
        datos = _leer_resumenes()      # releer bajo el lock: el ciclo pudo tocarlo
        resumenes = datos.setdefault("resumenes", {})
        for numeracion_rc in vencidas:
            entrada = resumenes.get(numeracion_rc)
            if entrada is None:
                continue
            if any(b in pendientes for b in entrada.get("boletas", [])):
                retenidas.append(numeracion_rc)
                continue
            resumenes.pop(numeracion_rc, None)
            podadas.append(numeracion_rc)
        datos["ultima_poda"] = ahora.strftime("%Y-%m-%d %H:%M:%S")
        _guardar_resumenes(datos)

    if podadas:
        logger.info(
            "Poda de %s: se sacaron %d resumen(es) resueltos de mas de %d dias; "
            "quedan %d.", os.path.basename(_RESUMENES_PATH), len(podadas),
            DIAS_RETENCION_RESUMENES, len(resumenes),
        )
    if retenidas:
        logger.warning(
            "%d resumen(es) viejos se conservan porque todavia tienen boletas en "
            "enviado=0: %s", len(retenidas), ", ".join(retenidas[:_MAX_BLOQUEADOS_LOG]),
        )


def _boletas_de_resumen(numeracion_rc: str) -> list:
    entrada = _leer_resumenes().get("resumenes", {}).get(numeracion_rc) or {}
    return entrada.get("boletas", [])


def _boletas_en_resumenes_activos(ruc_emisor: str) -> set:
    """
    Boletas que ya entraron en algún resumen y por lo tanto NO pueden entrar en
    otro. Solo se liberan cuando ese resumen se cerró, porque ahí ya quedaron en
    enviado=true y las filtra obtener_boletas_para_resumen() por su cuenta.

    En cualquier otro caso se retienen, incluso si el SFS no sabe nada del resumen:
    esa ausencia no distingue entre "nunca se entregó" y "se entregó y ya se
    limpió". Liberarlas ante la duda es lo que genera el peor error posible acá —
    las mismas boletas declaradas dos veces a SUNAT, que acepta ambos resúmenes sin
    notar que llevan los mismos comprobantes, y que solo se deshace con una
    comunicación de baja. Retenerlas de más, en cambio, se ve en el log y se
    resuelve sacando el resumen de resumenes.json.
    """
    en_vuelo = _docs_en_vuelo(ruc_emisor)
    activas = set()
    for numeracion_rc, entrada in _leer_resumenes().get("resumenes", {}).items():
        boletas = entrada.get("boletas", [])
        # Descartado: sus boletas ya volvieron a la cola a propósito, retenerlas acá
        # las dejaría sin poder entrar a ningún resumen nuevo. La entrada sigue en el
        # archivo solo para poder honrar un CDR que llegue tarde (ver
        # _olvidar_resumen), no para bloquear nada.
        #
        # Cerrado: sus boletas quedaron en enviado=1 y obtener_boletas_para_resumen()
        # ya las filtra por su cuenta. Hace falta mirarlo acá igual porque la fila del
        # SFS se limpia con el tiempo, y sin esta marca la entrada caía en la rama de
        # "sin rastro" de abajo y avisaba en cada ciclo de un resumen que terminó bien.
        if entrada.get("descartado") or entrada.get("cerrado"):
            continue
        situ, _ = en_vuelo.get((_TIPO_RC, numeracion_rc), ("", ""))
        if situ and situ not in _ESTADOS_CERRADOS:
            activas.update(boletas)
            continue
        if situ:
            continue  # cerrado: ya se resolvió, no hace falta ningún resguardo

        # Sin rastro en la bandeja del SFS no se puede saber qué pasó: puede que
        # nunca se haya entregado, o que se haya entregado y ya se limpiara. Ante
        # esa duda las boletas NO se liberan.
        #
        # Antes se liberaban pasado un lapso, y eso genero dos resumenes con las
        # mismas boletas mientras el SFS reiniciaba en bucle: SUNAT acepto los
        # dos, porque no detecta que lleven los mismos comprobantes. Declarar dos
        # veces solo se deshace con una comunicacion de baja. Una boleta trabada,
        # en cambio, se ve en el log y se destraba sacando su resumen de
        # resumenes.json: molesto, pero reversible.
        activas.update(boletas)
        if not _rdi_presente(ruc_emisor, numeracion_rc):
            _avisar_resumen_sin_rastro(numeracion_rc, entrada, boletas)
    return activas


def _rdi_presente(ruc_emisor: str, numeracion_rc: str) -> bool:
    """
    True si el .RDI del resumen sigue en DATA, o sea que aun no se proceso: esos
    archivos solo se borran cuando el documento se cierra.
    """
    base = _nombre_archivo_rc(ruc_emisor, numeracion_rc)
    return os.path.exists(os.path.join(SFS_DATA_DIR, f"{base}.RDI"))


def _reportar_resumenes_trabados(ruc_emisor: str):
    """
    Avisa por cada resumen que retiene boletas y no termina de resolverse.

    _avisar_resumen_sin_rastro() ya cubre el resumen que desaparecio de la bandeja
    del SFS, pero no el que sigue ahi en un estado que no avanza. Ese caso no
    generaba una sola linea: los resumenes no son filas de Comprobantes, asi que
    nunca llegan al bloque de BLOQUEADOS que arma el ciclo, y el unico rastro era el
    conteo de pendientes sin nada que lo explicara.

    Es exactamente lo que dejo pasar el incidente del 2026-09-06: siete resumenes
    trabados retuvieron 1239 boletas durante 12 horas sin un solo WARNING.

    Se avisa desde _GRACIA_REGISTRO_RC_SEG para no gritar por un resumen que acaba de
    generarse y todavia esta en curso normal.
    """
    en_vuelo = _docs_en_vuelo(ruc_emisor)
    trabados = []
    for numeracion_rc, entrada in _leer_resumenes().get("resumenes", {}).items():
        situ, obse = en_vuelo.get((_TIPO_RC, numeracion_rc), ("", ""))
        if not situ or situ in _ESTADOS_CERRADOS:
            continue          # sin rastro lo cubre _avisar_resumen_sin_rastro; cerrado no molesta
        try:
            generado = datetime.strptime(entrada.get("generado", ""), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        horas = (datetime.now() - generado).total_seconds() / 3600
        if horas * 3600 < _GRACIA_REGISTRO_RC_SEG:
            continue
        trabados.append((numeracion_rc, situ, len(entrada.get("boletas", [])), horas, obse))

    if not trabados:
        return
    retenidas = sum(t[2] for t in trabados)
    logger.warning(
        "%d resumen(es) sin resolverse retienen %d boleta(s), que no se pueden "
        "reagrupar hasta que se cierren:", len(trabados), retenidas,
    )
    for numeracion_rc, situ, cuantas, horas, obse in sorted(trabados, key=lambda t: -t[3]):
        logger.warning(
            "    %s [%s] — %d boleta(s), %.1f h sin cerrar: %s",
            numeracion_rc, _nombre_situ_rc(situ), cuantas, horas, obse or "sin detalle",
        )


def _nombre_situ_rc(situ: str) -> str:
    """
    Nombre del estado tal como se lee en un resumen, no en un comprobante.

    _NOMBRE_SITU traduce el '05' como "anulado", que es lo que significa para una
    factura. En un resumen ese estado lo deja el SFS cuando la consulta del ticket
    no le sirvio, y nada se anulo: mostrar "anulado" manda a buscar algo que no
    paso, justo en el aviso que existe para orientar a quien lo lee.
    """
    if situ == "05":
        return "05, consulta del ticket sin resolver"
    return _NOMBRE_SITU.get(situ, situ)


def _avisar_resumen_sin_rastro(numeracion_rc: str, entrada: dict, boletas: list):
    """Un resumen del que no queda ninguna señal necesita que alguien lo mire."""
    try:
        generado = datetime.strptime(entrada.get("generado", ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return
    if (datetime.now() - generado).total_seconds() < _GRACIA_REGISTRO_RC_SEG:
        return   # recien generado: es normal que todavia no haya rastro
    logger.warning(
        "El resumen %s no figura en el SFS ni tiene archivos en DATA. Sus %d "
        "boleta(s) quedan retenidas para no declararlas dos veces. Si se confirma "
        "que nunca llego a SUNAT, borrar su entrada de %s para que vuelvan a la cola.",
        numeracion_rc, len(boletas), os.path.basename(_RESUMENES_PATH),
    )


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


# Frases que solo aparecen cuando el envío ni siquiera llegó a SUNAT. La lista es a
# propósito corta y literal: ante la menor duda conviene gastar un reintento y que
# el comprobante termine bloqueado —alguien lo mira— antes que reencolarlo para
# siempre por un error que en realidad era de datos.
#
# El "Could not send Message" es el confirmado en produccion (corte del 2026-08-29):
# el SFS no pudo ni abrir la conversación con SUNAT. Los demás son las variantes de
# red que devuelve la misma capa.
#
# OJO con lo que NO va acá: el "0111 - No tiene el perfil para enviar comprobantes
# electronicos" también aterriza en '06', pero es una respuesta de SUNAT, no una
# falla de red. Tiene que gastar reintentos y terminar bloqueado, porque no se
# arregla esperando.
_SENALES_DE_RED = (
    "could not send message",
    "connection timed out",
    "connect timed out",
    "read timed out",
    "connection refused",
    "connection reset",
    "unknownhostexception",
    "sockettimeoutexception",
    "socketexception",
    "no route to host",
    "network is unreachable",
)


def _es_falla_de_red(motivo: str) -> bool:
    """
    True si el motivo del '06' es inequívocamente de comunicación.

    Separa las dos cosas que el SFS mete en el mismo estado: un dato mal armado
    —que reintentar no arregla— y un corte de red, donde el comprobante nunca salió
    y el mismo envío funciona apenas vuelve el servicio.
    """
    return any(s in _texto(motivo).lower() for s in _SENALES_DE_RED)


def _espera_de(numeracion: str) -> float:
    """Marca de tiempo (epoch) hasta la que este comprobante no se reintenta."""
    with _lock_reintentos:
        registro = _leer_reintentos().get(numeracion) or {}
    try:
        return float(registro.get("esperar_hasta", 0))
    except (TypeError, ValueError):
        return 0.0


def _anotar_espera_de_red(numeracion: str, tipo: str, motivo: str) -> tuple:
    """
    Registra un intento fallido por red y devuelve (cortes, minutos de espera).

    El contador va en 'cortes' y no en 'intentos' a propósito: 'intentos' es el
    presupuesto que agota un comprobante y lo bloquea, y una falla de red no debe
    gastarlo. Acá solo sirve para espaciar los reintentos.

    La espera se guarda en disco y no en memoria por el mismo motivo que el
    contador: PM2 reinicia el daemon solo, y un backoff en memoria volvería a cero
    en cada reinicio, martillando a SUNAT durante un corte largo.
    """
    with _lock_reintentos:
        datos = _leer_reintentos()
        registro = datos.get(numeracion) or {}
        cortes = int(registro.get("cortes", 0)) + 1
        # 1, 2, 4, 8, 15, 15... minutos. Arranca cerca del ciclo normal para que un
        # corte de segundos no demore el comprobante, y se aplana en 15 para no
        # dejarlo esperando media hora cuando el servicio ya volvió.
        minutos = min(2 ** (cortes - 1), _ESPERA_MAX_RED_MIN)
        registro.update({
            "tipo": tipo,
            "cortes": cortes,
            "ultimo": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "motivo": motivo or registro.get("motivo", ""),
            "esperar_hasta": time.time() + minutos * 60,
        })
        datos[numeracion] = registro
        _guardar_reintentos(datos)
        return cortes, minutos


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
        reintentados, agotados, esperando = [], [], []
        for num_docu, tip_docu, des_obse in rows:
            # Un '06' de red es otra cosa que un rechazo: el comprobante nunca llegó a
            # SUNAT, los datos están bien, y el mismo envío funciona apenas vuelve el
            # servicio. Gastarle presupuesto de reintentos lo bloqueaba en menos de 5
            # minutos frente a un corte de horas (produccion, 2026-08-29: 7 facturas
            # bloqueadas por un corte de 2 h, todas aceptadas despues sin tocarles un
            # dato). Se reencola sin tope, espaciado, y con una salvaguarda antes de
            # reenviar.
            if _es_falla_de_red(des_obse):
                falta = _espera_de(num_docu) - time.time()
                if falta > 0:
                    esperando.append((tip_docu, num_docu, falta / 60))
                    continue
                # Un "no se pudo enviar" no distingue entre "nunca salió" y "salió y
                # la respuesta se perdió". Reenviar el segundo caso duplica el
                # comprobante ante SUNAT, y eso solo se deshace con una nota de
                # crédito, así que hace falta una evidencia antes de tocar nada.
                #
                # Para un resumen esa evidencia NO es estado_en_sunat(): esa consulta
                # va contra billConsultService, que solo acepta comprobantes
                # individuales, y con tip_docu='RC' SUNAT contesta "0009: EL tipo de
                # comprobante debe de ser (01, 07, 08, ...)". RC no está en esa lista
                # y nunca va a estarlo, así que la respuesta no dice nada del
                # documento y el resumen quedaba reintentando una consulta imposible
                # para siempre —RC-20260906-016 acumuló 54 consultas así, con sus
                # boletas sin declarar—.
                #
                # La evidencia para un resumen es el ticket: lo escribe el SFS cuando
                # SUNAT responde a sendSummary, así que su ausencia significa que el
                # envío no llegó. Es el equivalente del 0127 que autoriza a reenviar
                # un comprobante suelto. Al revés, un resumen CON ticket ya fue
                # recibido y no se reenvía por acá: lo resuelve
                # recuperar_cdr_resumenes() consultando ese ticket.
                if tip_docu == _TIPO_RC:
                    if _ticket_de_resumen(ruc_emisor, num_docu):
                        cortes, minutos = _anotar_espera_de_red(
                            num_docu, tip_docu, _texto(des_obse))
                        esperando.append((tip_docu, num_docu, minutos))
                        continue
                    # Sin ticket: SUNAT no lo recibió y se puede volver a armar.
                else:
                    estado, cdr, mensaje = estado_en_sunat(ruc_emisor, tip_docu, num_docu)
                    if estado == "registrado":
                        _guardar_cdr(ruc_emisor, tip_docu, num_docu, cdr, mensaje)
                        continue
                    if estado != "no_registrado":
                        cortes, minutos = _anotar_espera_de_red(
                            num_docu, tip_docu, _texto(des_obse))
                        esperando.append((tip_docu, num_docu, minutos))
                        continue
                cortes, minutos = _anotar_espera_de_red(num_docu, tip_docu, _texto(des_obse))
                if tip_docu == _TIPO_RC:
                    # Un resumen no es una fila de Comprobantes: marcar_enviado() con
                    # su numeración no matchea nada. Lo que hay que devolver a la cola
                    # son sus boletas, y para eso alcanza con olvidar el resumen —el
                    # ciclo siguiente las reagrupa en uno nuevo—. Sin esto, borrar la
                    # fila del SFS dejaba al resumen sin rastro y sus boletas seguían
                    # retenidas por algo que ya no existía.
                    boletas_libres = _olvidar_resumen(num_docu)
                    logger.warning(
                        "El resumen %s no llegó a obtener ticket, así que SUNAT no lo "
                        "recibió: se descarta y sus %d boleta(s) vuelven a la cola "
                        "para armar uno nuevo.", num_docu, len(boletas_libres),
                    )
                else:
                    _bd().marcar_enviado(conn, num_docu, enviado=ENVIADO_PENDIENTE,
                                         limpiar_error=False)
                sfs.execute(
                    f"DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                    f"AND IND_SITU IN ({marcas})",
                    (ruc_emisor, tip_docu, num_docu, *_ESTADOS_ERROR),
                )
                reintentados.append((tip_docu, num_docu, f"corte {cortes}"))
                continue

            # Se consulta antes de sumar: al agotarse, el documento se queda en '10'
            # y esta rama corre en cada ciclo. Sumando siempre, el contador crecería
            # sin sentido y reescribiría el archivo cada 60 segundos para siempre.
            if _reintentos_de(num_docu) >= MAX_REINTENTOS_RECHAZO:
                # Se deja la fila en DOCUMENTO: así el comprobante sigue contando como
                # "en vuelo" —no se regenera— y el reporte de bloqueados lo levanta.
                agotados.append((tip_docu, num_docu))
                continue
            intentos = f"{_contar_reintento(num_docu, tip_docu, _texto(des_obse))}/{MAX_REINTENTOS_RECHAZO}"
            # Vuelve a la cola sin tocar errors: el motivo del rechazo tiene que
            # seguir a la vista mientras se reintenta.
            _bd().marcar_enviado(conn, num_docu, enviado=ENVIADO_PENDIENTE,
                                 limpiar_error=False)
            sfs.execute(
                f"DELETE FROM DOCUMENTO WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? "
                f"AND IND_SITU IN ({marcas})",
                (ruc_emisor, tip_docu, num_docu, *_ESTADOS_ERROR),
            )
            reintentados.append((tip_docu, num_docu, intentos))

    if reintentados:
        logger.info(
            "%d comprobante(s) vuelven a la cola: %s",
            len(reintentados),
            ", ".join(f"{t}-{n} ({i})" for t, n, i in reintentados),
        )
    if esperando:
        # A nivel INFO y con su propio texto: estos NO requieren que nadie haga nada,
        # solo que vuelva el servicio. Mezclarlos con los bloqueados mandaba a buscar
        # una corrección manual que no existía.
        logger.info(
            "%d comprobante(s) esperando que vuelva SUNAT; reintentan solos: %s",
            len(esperando),
            ", ".join(f"{t}-{n} (en {m:.0f} min)" for t, n, m in esperando),
        )
    if agotados:
        # Reenviar de nuevo daría el mismo rechazo: los datos son idénticos. Solo
        # llegan acá los que NO son falla de red — esos no gastan presupuesto.
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
    # El resumen diario (RC-YYYYMMDD-NNN) tiene solo 2 letras antes del guión, así que
    # necesita su propia alternativa: nunca calzaría con las 4 exigidas por la otra.
    m = re.search(rf"{_TIPO_RC}-\d{{8}}-\d+|[A-Z][A-Z0-9]{{3}}-\d+", _texto(texto))
    return m.group(0) if m else None


def _respuestas_por_documento(root) -> list:
    """
    [(numeracion, codigo, descripcion)] de cada <cac:DocumentResponse> del CDR.

    El esquema los declara con maxOccurs="unbounded": un CDR de resumen puede
    traer uno por el resumen entero y otro por cada boleta que SUNAT observe. Sin
    recorrerlos todos, esas observaciones se pierden — el comprobante queda
    aceptado y nadie se entera de que una línea salió con reparos.
    """
    respuestas = []
    for elem in root.iter():
        if not isinstance(elem.tag, str) or elem.tag.split("}")[-1] != "DocumentResponse":
            continue
        datos = {}
        for hijo in elem.iter():
            if not isinstance(hijo.tag, str):
                continue
            tag = hijo.tag.split("}")[-1].lower()
            texto = _texto(hijo.text)
            if texto and tag in ("referenceid", "responsecode", "description") and tag not in datos:
                datos[tag] = texto
        if datos:
            respuestas.append((
                _extraer_numeracion(datos.get("referenceid", "")),
                datos.get("responsecode"),
                datos.get("description"),
            ))
    return respuestas


def _reconciliar_numeracion(del_xml: str | None, nombre_archivo: str) -> str | None:
    """
    Numeración canónica del comprobante, cuando el XML y el nombre del archivo no
    coinciden.

    SUNAT escribe el número distinto según por dónde llegue el CDR. El de sendBill
    —el envío normal, que entrega el SFS— trae 'F003-009595'. El de getStatusCdr
    —la consulta que hace estado_en_sunat()— trae '20605858601-01-F003-9571': con
    prefijo de RUC y tipo, y el correlativo SIN los ceros a la izquierda. Los dos
    son CDR legítimos y firmados; simplemente no usan el mismo formato.

    De ahí que el XML sirva para el estado y los códigos, pero no para la identidad:
    _extraer_numeracion() sacaba 'F003-9571' de ese segundo formato y no existe
    ninguna fila así en Comprobantes, que la guarda como 'F003-009571'. El nombre
    del archivo, en cambio, es canónico en los dos caminos: lo arma _guardar_cdr()
    desde el NUM_DOCU del SFS, y el SFS lo arma desde su propia tabla.

    No se rellena con ceros a un ancho fijo a propósito: los 6 dígitos son
    convención de esta aplicación, no de SUNAT —que admite hasta 8—, y fijarlos acá
    rompería con cualquier otro emisor. Se comparan los correlativos como enteros,
    que es la única equivalencia que vale sin importar el relleno.
    """
    del_nombre = _extraer_numeracion(nombre_archivo)
    if not del_nombre:
        return del_xml
    if not del_xml or del_xml == del_nombre:
        return del_nombre

    serie_xml, _, corr_xml = del_xml.partition("-")
    serie_arch, _, corr_arch = del_nombre.partition("-")
    # Los resúmenes (RC-YYYYMMDD-NNN) no entran acá: su correlativo no es un entero
    # suelto, así que isdigit() falla y se respeta lo que dijo el XML.
    if serie_xml == serie_arch and corr_xml.isdigit() and corr_arch.isdigit() \
            and int(corr_xml) == int(corr_arch):
        return del_nombre
    return del_xml


def _datos_del_nombre_cdr(nombre: str) -> tuple:
    """
    (ruc, tipo) a partir del nombre del CDR: 'R20605858601-01-F003-009571.zip'.

    Hacen falta para cerrar la fila en la bandeja del SFS, que se identifica por
    NUM_RUC + TIP_DOCU + NUM_DOCU. Devuelve (None, None) si el nombre no tiene esa
    forma, y quien llama simplemente no cierra nada.
    """
    m = re.match(r"R(\d{11})-([A-Z0-9]{2})-", _texto(nombre))
    return (m.group(1), m.group(2)) if m else (None, None)


def _cerrar_documento_en_sfs(ruc: str, tipo: str, numeracion: str):
    """
    Da por enviado y aceptado un documento en la bandeja del SFS.

    Solo hace falta cuando el CDR no llegó por el camino del SFS sino que lo bajó
    el daemon de SUNAT (ver _guardar_cdr): en ese caso la fila queda como la dejó
    el error de red, en IND_SITU='06' y con FEC_ENVI vacía. Sin esto pasaban dos
    cosas, las dos vistas en producción el 2026-09-03: resetear_rechazados() volvía
    a levantar la fila en el ciclo siguiente —consultaba a SUNAT otra vez, bajaba
    el mismo CDR, y así cada 60 segundos durante casi cuatro horas—, y la bandeja
    mostraba el comprobante como "Con Errores" pese a estar aceptado en SUNAT.

    El WHERE filtra por los estados de error a propósito: si la fila ya está
    cerrada, el UPDATE no afecta ninguna y el segundo pase del mismo CDR —watchdog
    y barrido periódico pueden verlo dos veces— no revierte nada.
    """
    if not (ruc and tipo and numeracion) or not os.path.exists(SFS_BD_PATH):
        return
    marcas = _marcas(len(_ESTADOS_ERROR))
    try:
        with _sfs_bd(escritura=True) as sfs:
            sfs.execute(
                f"UPDATE DOCUMENTO SET IND_SITU='03', FEC_ENVI=?, DES_OBSE='-' "
                f"WHERE NUM_RUC=? AND TIP_DOCU=? AND NUM_DOCU=? AND IND_SITU IN ({marcas})",
                (datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
                 ruc, tipo, numeracion, *_ESTADOS_ERROR),
            )
    except sqlite3.Error:
        logger.exception(
            "No se pudo cerrar %s-%s en la bandeja del SFS; el comprobante quedó "
            "bien cerrado en la BD igual.", tipo, numeracion,
        )


def parsear_xml_cdr(fuente) -> dict:
    res = {"numeracion": None, "codigo": None, "descripcion": None,
           "status": "PENDIENTE", "lineas": []}
    try:
        root = ET.fromstring(fuente) if isinstance(fuente, bytes) else ET.parse(fuente).getroot()
        res["lineas"] = _respuestas_por_documento(root)
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


def _procesar_lineas_de_resumen(conn, numeracion_rc: str, boletas: list, parsed: dict) -> tuple:
    """
    Separa las boletas de un resumen ACEPTADO en limpias/excluidas según el código
    de su propia línea de respuesta. Devuelve (limpias, excluidas).

    Mismo criterio que ya usa parsear_xml_cdr() para el documento entero —"El
    ResponseCode manda: SUNAT solo devuelve 0 cuando acepta"—, aplicado ahora por
    línea: un código de solo ceros es la aceptación limpia; cualquier otro código,
    aunque el CDR lo etiquete como "observación", significa que SUNAT no registró
    esa boleta puntual, aunque sí haya aceptado el resumen que la contenía.
    Marcarla enviado=1 junto con las demás sería declararla aceptada cuando no lo
    está.

    Las excluidas no se pierden: quedan en enviado=0 con el motivo guardado, y
    vuelven a proponerse en un resumen futuro (obtener_boletas_para_resumen) hasta
    agotar MAX_REINTENTOS_RECHAZO.
    """
    incluidas = set(boletas)
    codigos = {
        num: (cod, desc) for num, cod, desc in parsed.get("lineas", [])
        # La respuesta del resumen entero no es una observación de línea, y un
        # código de solo ceros es la aceptación limpia.
        if num in incluidas and (cod or "").strip("0") != ""
    }
    if not codigos:
        return boletas, []

    excluidas = [b for b in boletas if b in codigos]
    limpias = [b for b in boletas if b not in codigos]

    agotadas = []
    for num in excluidas:
        cod, desc = codigos[num]
        intentos = _contar_reintento(num, "03", desc or f"código {cod}")
        detalle = (
            f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Excluida del resumen {numeracion_rc} "
            f"(intento {intentos}/{MAX_REINTENTOS_RECHAZO})"
        )
        if cod:
            detalle += f" — código {cod}"
        if desc:
            detalle += f": {desc}"
        _escribir_bd(_bd().guardar_error, conn, num, detalle[:_MAX_ERRORS_SQL])
        if intentos >= MAX_REINTENTOS_RECHAZO:
            agotadas.append(num)

    logger.warning(
        "Resumen %s aceptado, pero SUNAT devolvió código en %d boleta(s); no se "
        "marcan enviado=1 y quedan pendientes para un resumen futuro: %s",
        numeracion_rc, len(excluidas), ", ".join(excluidas),
    )
    if agotadas:
        logger.error(
            "%d boleta(s) agotaron los %d reenvíos dentro de un resumen y NO se "
            "reincluirán hasta que se corrija el dato observado: %s",
            len(agotadas), MAX_REINTENTOS_RECHAZO, ", ".join(agotadas),
        )
    return limpias, excluidas


def _limpiar_reintentos(numeraciones: list):
    """
    Igual que _limpiar_reintento() pero en lote: una sola lectura/escritura de
    reintentos.json para todo un resumen, en vez de una por boleta.
    """
    if not numeraciones:
        return
    with _lock_reintentos:
        datos = _leer_reintentos()
        tocado = False
        for num in numeraciones:
            if datos.pop(num, None) is not None:
                tocado = True
        if tocado:
            _guardar_reintentos(datos)


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

    if numeracion.startswith(f"{_TIPO_RC}-"):
        # Un resumen no es una fila de Factura: agrupa muchas boletas, así que el
        # cierre es un fan-out a todas las que se guardaron en resumenes.json cuando
        # se generó, no un UPDATE de una sola fila.
        boletas = _boletas_de_resumen(numeracion)
        descartado = _resumen_descartado(numeracion)
        if not boletas:
            # Antes de darse por vencido, reconstruir desde el XML firmado: las
            # entradas que borró la versión anterior de _olvidar_resumen() ya no están,
            # y sus CDR tardíos tienen que poder cerrar sus boletas igual.
            boletas = _boletas_desde_firma(EMISOR_RUC_OVERRIDE, numeracion)
            if boletas:
                logger.warning(
                    "El resumen %s no figura en %s; sus %d boleta(s) se reconstruyeron "
                    "desde el XML firmado en FIRMA/.",
                    numeracion, os.path.basename(_RESUMENES_PATH), len(boletas),
                )
        if not boletas:
            logger.error(
                "CDR aceptado del resumen %s pero no hay boletas registradas para él "
                "en %s ni se pudo reconstruir desde FIRMA/; quedan en enviado=0.",
                numeracion, os.path.basename(_RESUMENES_PATH),
            )
            return False

        if descartado:
            # SUNAT sí lo había recibido: se lo descartó dando por hecho que no, porque
            # el envío falló sin devolver ticket. Ahora hay que avisarlo fuerte, porque
            # si esas boletas ya viajaron en otro resumen aceptado están declaradas dos
            # veces ante SUNAT y eso solo se deshace con una comunicación de baja.
            repiten = _resumenes_que_repiten(numeracion, boletas)
            logger.error(
                "El resumen %s se había descartado el %s por no obtener ticket, pero "
                "SUNAT lo aceptó: sus %d boleta(s) se cierran igual.%s",
                numeracion, descartado, len(boletas),
                (" ATENCIÓN: esas boletas también se declararon en %s, así que hay un "
                 "duplicado ante SUNAT que requiere comunicación de baja."
                 % ", ".join(repiten)) if repiten else "",
            )

        # Separa las que SUNAT registró de verdad (limpias) de las que su propia
        # línea vino con código — esas NO se marcan enviado=1 aunque el resumen
        # entero se haya aceptado. Ver _procesar_lineas_de_resumen().
        limpias, excluidas = _procesar_lineas_de_resumen(conn, numeracion, boletas, parsed)
        filas = _escribir_bd(_bd().marcar_enviados, conn, limpias) if limpias else 0
        if limpias and filas == 0:
            logger.error(
                "CDR aceptado del resumen %s pero ninguna de sus boletas coincide en la "
                "BD; quedan en enviado=0.", numeracion,
            )
            return False

        _limpiar_reintento(numeracion)
        _limpiar_reintentos(limpias)
        # Deja constancia de que este resumen ya cerró: es lo que distingue una entrada
        # terminada de una a medias cuando su fila del SFS ya se limpió, y sin eso la
        # poda no sabría cuál puede sacar del archivo.
        _marcar_resumen_cerrado(numeracion, boletas)
        # Recién ahora el resumen esta terminado de verdad: sus boletas limpias ya
        # quedaron cerradas. Marcarlo antes liberaba las boletas mientras todavia
        # figuraban pendientes, y se generaba otro resumen con ellas.
        _cerrar_resumen_en_sfs(EMISOR_RUC_OVERRIDE, numeracion, _veredicto_cdr(parsed))
        logger.info(
            "Resumen %s aceptado: %d boleta(s) marcadas enviado=1%s.",
            numeracion, filas,
            f"; {len(excluidas)} quedan pendientes por código de línea" if excluidas else "",
        )
        return True

    # errors se limpia junto con la aceptación: si el comprobante había sido rechazado
    # antes, el motivo viejo ya no aplica.
    filas = _escribir_bd(_bd().marcar_enviado, conn, numeracion)
    if filas > 0:
        _limpiar_reintento(numeracion)
        return True
    if filas == 0:
        # SUNAT aceptó algo que no está en Comprobante: numeración con otro formato,
        # comprobante borrado, o CDR de otro emisor. Silenciarlo dejaba el documento
        # en enviado=0 y el ZIP archivado como procesado, o sea perdido.
        logger.error(
            "CDR aceptado de %s pero ningún comprobante coincide en la BD; "
            "queda en enviado=0.", numeracion,
        )
    return False


def _registrar_error_cdr(conn, numeracion: str, parsed: dict) -> bool:
    """
    Deja el motivo del rechazo en Comprobante.errors. Sin esto el código de SUNAT
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

    if numeracion.startswith(f"{_TIPO_RC}-"):
        boletas = _boletas_de_resumen(numeracion)
        if not boletas:
            return False
        filas = _escribir_bd(_bd().guardar_error_varios, conn, boletas,
                             detalle[:_MAX_ERRORS_SQL])
        # Aunque haya sido rechazado, el ticket ya se consumio: dejarlo abierto
        # haria que se lo siguiera consultando en vano en cada ciclo. El motivo
        # del rechazo queda en Factura.errors, que es donde se consulta —y tambien
        # en la bandeja del SFS, que es lo primero que alguien mira: este es
        # justamente el camino que rotulaba "Aceptado" un resumen rechazado.
        _cerrar_resumen_en_sfs(EMISOR_RUC_OVERRIDE, numeracion, _veredicto_cdr(parsed))
        return filas > 0

    filas = _escribir_bd(_bd().guardar_error, conn, numeracion,
                         detalle[:_MAX_ERRORS_SQL])
    return filas > 0


def _archivo_estable(ruta: str, intentos: int = 5, espera: float = 0.5) -> bool:
    """
    True cuando el tamaño del archivo dejó de cambiar. SUNAT/SFS deja el ZIP en RPTA
    mientras todavía lo escribe y watchdog avisa apenas se crea: abrirlo de inmediato
    daba BadZipFile —y lo mandaba a errores/— sobre un archivo que estaba sano.

    Un archivo que se queda en 0 bytes nunca se da por estable, y eso es correcto: no
    hay nada que abrir. Distinguir ese caso de uno que todavía crece es tarea de quien
    llama (ver _archivo_abandonado), porque acá no se puede saber cuánto lleva así.
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


def _archivo_abandonado(ruta: str) -> bool:
    """
    True si el archivo lleva demasiado tiempo vacío como para seguir esperándolo.

    Un ZIP que se corta a medio escribir —un corte del lado del SFS, disco lleno—
    queda en 0 bytes para siempre. _archivo_estable() nunca lo da por bueno, asi que
    el barrido lo saltaba en cada ciclo con el mismo INFO de "aún se está escribiendo"
    sin que nadie lo resolviera. Visto en produccion el 2026-09-05: horas repitiendo
    esa linea.

    Y no era solo ruido en el log: _tiene_cdr() solo mira que el archivo exista, asi
    que ese ZIP vacio hacia que recuperar_cdr_pendientes() diera por recuperado el CDR
    y no volviera a consultarle a SUNAT. El comprobante quedaba en enviado=0 aunque
    SUNAT lo hubiera aceptado, sin ninguna via de salida.

    El umbral de tiempo es lo que separa un archivo abandonado de uno que recien
    empieza: los dos miden 0 bytes, y la unica diferencia es hace cuanto.
    """
    try:
        if os.path.getsize(ruta) > 0:
            return False
        edad = time.time() - os.path.getmtime(ruta)
    except OSError:
        return False
    return edad > MINUTOS_CDR_VACIO * 60


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
                    # Un archivo vacío que ya no va a completarse no puede quedarse en
                    # RPTA: además de repetir este aviso para siempre, le hace creer a
                    # _tiene_cdr() que el CDR ya está y bloquea la reconsulta a SUNAT.
                    if _archivo_abandonado(ruta):
                        logger.warning(
                            "CDR %s lleva más de %d min en 0 bytes; quedó a medio escribir. "
                            "Se aparta en errores/ para que se pueda volver a consultar a SUNAT.",
                            nombre, MINUTOS_CDR_VACIO,
                        )
                        _mover(ruta, DIR_ERRORES)
                        err += 1
                        continue
                    # Lo retoma el barrido periódico de hilo_cdr; no cuenta como error.
                    logger.info("CDR %s aún se está escribiendo; se retoma luego.", nombre)
                    continue
                if ruta.lower().endswith(".zip"):
                    with zipfile.ZipFile(ruta) as z:
                        xml_names = [n for n in z.namelist() if n.lower().endswith(".xml")]
                        if not xml_names:
                            _mover(ruta, DIR_ERRORES); err += 1; continue
                        parsed = parsear_xml_cdr(z.read(xml_names[0]))
                    # No alcanza con completar la numeración cuando falta: el CDR de
                    # una consulta trae una que existe pero no casa con la BD, así que
                    # hay que reconciliarla contra el nombre del archivo.
                    parsed["numeracion"] = _reconciliar_numeracion(parsed["numeracion"], nombre)
                else:
                    parsed = parsear_xml_cdr(ruta)

                num = parsed["numeracion"]
                logger.info("CDR %s | %s [%s]", nombre, num, parsed["status"])

                if parsed["status"] in _CDR_ACEPTADOS:
                    if _actualizar_sql_cdr(conn, num, parsed):
                        ok += 1
                        # Recién con el comprobante ya cerrado en la BD de la
                        # aplicación se limpia la fila del SFS. Al revés —limpiarla
                        # al depositar el CDR— se borraba el error visible aunque el
                        # cierre fallara después, y el comprobante quedaba en
                        # enviado=0 sin que nadie se enterara.
                        ruc_cdr, tipo_cdr = _datos_del_nombre_cdr(nombre)
                        _cerrar_documento_en_sfs(ruc_cdr, tipo_cdr, num)
                        _mover(ruta, DIR_PROCESADOS)
                    else:
                        # Aceptado por SUNAT pero no se pudo cerrar en la BD
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

        # Antes de leer una sola fecha: medir con qué reloj las guarda la aplicación.
        # De esto depende qué día se le declara a SUNAT (ver detectar_desfase_bd).
        detectar_desfase_bd(conn)

        # Primero que el SFS relea DATA: todo lo que sigue —qué está en vuelo, qué
        # falta activar, qué se puede limpiar— se decide mirando su bandeja, y sin
        # esto no refleja lo que quedó escrito en ciclos anteriores.
        sincronizar_bandeja_sfs()

        resetear_rechazados(conn, ruc_emisor)

        # Antes de decidir qué generar: si algo se envió y su CDR nunca volvió
        # —típicamente por un corte de conexión—, preguntarle a SUNAT si lo tiene.
        # Y los resúmenes ya enviados esperan su CDR detrás de un ticket, que hay
        # que consultar aparte: SUNAT no lo devuelve en el momento del envío.
        if SOL_USUARIO and SOL_CLAVE:
            recuperar_cdr_pendientes(ruc_emisor)
            recuperar_cdr_resumenes(ruc_emisor)

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
                    # Un '06' de red no está trabado: lo reintenta resetear_rechazados()
                    # solo, apenas vuelva el servicio. Reportarlo como BLOQUEADO mandaba
                    # a buscar una corrección manual que no hacía falta.
                    if situ in _ESTADOS_BLOQUEADO and not _es_falla_de_red(obse):
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

        # Las boletas no entran al loop de arriba (ver obtener_comprobantes_pendientes):
        # se agrupan acá en un resumen diario, que de ahí en más sigue el mismo
        # camino que cualquier otro documento (activar_procesamiento_sfs, etc.).
        try:
            resumen_doc = generar_resumen_diario(conn, ruc_emisor)
            if resumen_doc:
                docs_generados.append(resumen_doc)
        except Exception:
            logger.exception("Error generando el resumen diario de boletas")

        # Mantenimiento de resumenes.json, no parte del flujo: se hace después de
        # generar para que una falla acá nunca impida emitir un resumen, y por su
        # cuenta corre una sola vez al día (ver _podar_resumenes).
        try:
            _podar_resumenes(conn)
        except Exception:
            logger.exception("Error podando %s", os.path.basename(_RESUMENES_PATH))

        _reportar_clasificacion(fuera_alcance, omitidos, bloqueados)
        # Va aparte porque los resúmenes no son filas de Comprobantes y por eso nunca
        # entran en la lista de bloqueados que arma el bucle de arriba.
        _reportar_resumenes_trabados(ruc_emisor)

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
    logger.info("  BASE DE DATOS: %s", _url_sin_clave(DATABASE_URL))
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

