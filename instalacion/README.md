# Instalación en un cliente nuevo

Todo se hace desde **`FacturadorSetup.exe`**: un solo ejecutable con menú, que lleva adentro su propio Python. Por eso corre en una PC recién formateada, donde todavía no hay nada instalado.

```
   1  Instalar el entorno (prerrequisitos, PM2, SFS)
   2  Configurar un cliente
   3  Verificar la instalacion
   4  Pasar a produccion
```

Están separados a propósito: cuando a un cliente le vence el certificado —que pasa— no hay que reinstalar nada, se corre solo la opción 2.

## Antes de empezar

Hay que tener a mano, del cliente:

- RUC, razón social y dirección fiscal **con ubigeo** (6 dígitos)
- Usuario y clave **SOL secundarios** (no los principales)
- El **certificado digital** `.p12` o `.pfx` y su contraseña
- La `DATABASE_URL` de la aplicación

Python, Java y Node los instala el propio programa con `winget`.

## El procedimiento

1. Copiar la carpeta del proyecto a la PC (clonar el repositorio o descomprimir el ZIP)
2. Doble click en **`FacturadorSetup.exe`**
3. Opción **1** — instala prerrequisitos, PM2 y descarga el SFS (~90 MB)
4. Opción **2** — pide los datos del cliente y deja todo andando

Al terminar el paso 4 **la instalación queda apuntando a beta**. Eso es deliberado:

1. Emitir un comprobante de prueba y confirmar que SUNAT lo acepte
2. Borrar los comprobantes de prueba de la base de la aplicación
3. Recién entonces, opción **4** — pasar a producción

**Por qué no instalar directo en producción.** Si algo está mal configurado y un comprobante real sale contra beta, el daemon lo marca como `enviado=true` en la base pero ante SUNAT no existe — y después nada delata la diferencia. Es el peor error posible de este sistema, y arrancar en beta lo vuelve imposible.

> **La primera vez que se abre en una PC nueva, Windows muestra "Windows protegió su PC".** Es SmartScreen, porque el ejecutable no está firmado: *Más información → Ejecutar de todas formas*. Evitarlo requiere un certificado de firma de código (200–400 USD al año).

## Lo que el instalador no hace, y por qué

**No trae el certificado.** Es la clave privada con la que se firman los comprobantes: copiarlo entre clientes sería darle a uno la capacidad de facturar a nombre de otro. Lo aporta cada cliente al instalar.

**No inventa las claves SOL.** Se le pasan al SFS para que las encripte él, igual que si se tipearan en su pantalla. Replicar su algoritmo sería frágil y se rompería con cualquier actualización suya.

**No prende el temporizador del SFS.** Sus jobs internos de generar y enviar harían por su cuenta lo mismo que el daemon hace por REST, compitiendo por los mismos documentos.

## Versión del SFS

Se instala la **2.1**. SUNAT publica hasta la 2.4 en
`http://www2.sunat.gob.pe/facturador/SFS_v-<versión>.zip` (descarga pública, sin clave SOL; el nombre lleva un guión después de la `v`).

Se fija la 2.1 porque es la única contra la que se verificó el formato de los archivos planos que genera el daemon, incluido el del resumen diario de boletas. Actualizar es una decisión aparte: hay que volver a probar la emisión de cada tipo de comprobante.

## Diagnóstico

La opción **3** revisa prerrequisitos, `.env`, la base de la aplicación, la configuración del SFS, **si apunta a producción o beta**, el certificado, los procesos de PM2 y el trabajo pendiente. No modifica nada, y no escribe en `facturador.log` — que es donde se investiga qué pasó con un comprobante real.

## Dos instalaciones sobre la misma base

Dos daemons apuntando a la misma `DATABASE_URL` **compiten por los mismos comprobantes**: el que llegue primero toma cada pendiente, lo envía y lo marca `enviado=true`; el otro ya no lo ve.

Mientras se prueba eso es solo ruido, pero con una base en uso real significa que un comprobante puede salir por la instalación equivocada —a beta, sin validez fiscal— y quedar marcado como enviado. Antes de que un cliente empiece a facturar de verdad, una sola instalación por base.

## Compilar el ejecutable

Solo hace falta al cambiar el código del instalador:

```
python -m pip install pyinstaller
python construir.py
```

Deja `FacturadorSetup.exe` en esta carpeta. No se versiona: se genera al publicar.

## Los archivos

| Archivo | Qué es |
|---|---|
| `instalador.py` | El menú y el flujo de cada operación |
| `sistema.py` | Prerrequisitos, winget, PM2, descarga del SFS |
| `contribuyente.py` | Validaciones, carga en el SFS, `.env`, ambiente |
| `chequeos.py` | El diagnóstico |
| `_limpiar_contribuyente.py` | Borra los datos del contribuyente anterior |
| `construir.py` | Compila el `.exe` |
