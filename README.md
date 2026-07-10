# Facturador

Daemon de Facturación Electrónica SUNAT (SFS v2.1).

Corre en segundo plano y envía facturas/boletas a SUNAT automáticamente: lee los comprobantes pendientes de una base de datos SQL Server, genera los archivos que necesita el SFS (Sistema de Facturación SUNAT), los envía al facturador local y procesa las respuestas (CDR) de SUNAT.

Corre dos hilos en paralelo:
- **Hilo Generador**: cada `INTERVALO_GENERACION_SEG` segundos (60s por defecto) revisa si hay comprobantes por enviar.
- **Hilo CDR**: monitorea en tiempo real la carpeta `RPTA` y procesa el ZIP en cuanto SUNAT responde.

## Requisitos

- Python 3.10+
- SQL Server accesible (driver ODBC instalado)
- SFS v2.1 instalado y corriendo localmente
- [PM2](https://pm2.keymetrics.io/) (opcional, para gestionar el proceso)

## Instalación

```bash
pip install -r requirements.txt
```

## Configuración

Crea un archivo `.env` en la raíz del proyecto (no se versiona) con estas variables:

```env
# SQL Server
DB_DRIVER={SQL Server}
DB_SERVER=.\SQLEXPRESS
DB_DATABASE=AUXILIAR
DB_TRUSTED=yes

# Facturador SFS
SFS_BASE_URL=http://localhost:9000
SFS_DATA_DIR=C:\SFS_v2.1\sunat_archivos\sfs\DATA
SFS_RPTA_DIR=C:\SFS_v2.1\sunat_archivos\sfs\RPTA
SFS_BD_PATH=C:\SFS_v2.1\bd\BDFacturador.db

# Intervalo de polling en segundos
INTERVALO_GENERACION_SEG=60

# RUC del emisor (dejar vacío para leerlo de la BD)
EMISOR_RUC=
```

## Uso

Ejecución directa:

```bash
python main.py
```

Con PM2 (usando `sfs.config.js`):

```bash
pm2 start sfs.config.js
```

Los logs se escriben en `facturador.log` y, si se usa PM2, también en `logs/out.log` / `logs/error.log`.

## Flujo del comprobante

```
SQL Server (enviado=0)
        ↓
ciclo_generacion() cada 60s
        ↓
Genera .cab .det .tri .ley .PAG en DATA
        ↓
Envía al SFS local → XML firmado → SUNAT
        ↓
SUNAT responde con CDR (ZIP) en RPTA
        ↓
Hilo CDR detecta el ZIP y lo procesa
        ↓
Si ACEPTADO → enviado=1 en SQL Server, ZIP movido a RPTA/procesados/
```
