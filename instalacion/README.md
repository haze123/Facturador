# Instalador

Un solo ejecutable con menú, `FacturadorSetup.exe`, que lleva adentro su propio Python. Por eso corre en una PC recién formateada, donde todavía no hay nada instalado.

```
   1  Instalar el entorno (prerrequisitos, PM2, SFS)
   2  Configurar un cliente
   3  Verificar la instalacion
   4  Pasar a produccion
```

**El procedimiento paso a paso está en el manual en PDF**, que se distribuye aparte del repositorio. Este documento cubre lo demás: por qué el instalador está armado así y qué hacer cuando algo se sale del camino.

Las cuatro opciones están separadas a propósito: cuando a un cliente le vence el certificado —que pasa— no hay que reinstalar nada, se corre solo la opción 2.

## Lo que el instalador no hace, y por qué

**No trae el certificado.** Es la clave privada con la que se firman los comprobantes: copiarlo entre clientes sería darle a uno la capacidad de facturar a nombre de otro. Lo aporta cada cliente al instalar.

**No inventa las claves SOL.** Se le pasan al SFS para que las encripte él, igual que si se tipearan en su pantalla. Replicar su algoritmo sería frágil y se rompería con cualquier actualización suya.

**No prende el temporizador del SFS.** Sus jobs internos de generar y enviar harían por su cuenta lo mismo que el daemon hace por REST, compitiendo por los mismos documentos.

**No firma el ejecutable.** Por eso Windows muestra *"Windows protegió su PC"* la primera vez en cada máquina: *Más información → Ejecutar de todas formas*. Evitarlo requiere un certificado de firma de código, entre 200 y 400 USD al año.

## Versión del SFS

Se instala la **2.1**. SUNAT publica hasta la 2.4 en
`http://www2.sunat.gob.pe/facturador/SFS_v-<versión>.zip` (descarga pública, sin clave SOL; el nombre lleva un guión después de la `v`).

Se fija la 2.1 porque es la única contra la que se verificó el formato de los archivos planos que genera el daemon, incluido el del resumen diario de boletas. Actualizar es una decisión aparte: hay que volver a probar la emisión de cada tipo de comprobante.

## Dos instalaciones sobre la misma base

Dos daemons apuntando a la misma `DATABASE_URL` **compiten por los mismos comprobantes**: el que llegue primero toma cada pendiente, lo envía y lo marca `enviado=true`; el otro ya no lo ve.

Mientras se prueba eso es solo ruido, pero con una base en uso real significa que un comprobante puede salir por la instalación equivocada —a beta, sin validez fiscal— y quedar marcado como enviado. Antes de que un cliente empiece a facturar de verdad, una sola instalación por base.

## Reutilizar una PC que ya tenía otro cliente

La opción 2 lo detecta y pregunta antes de borrar nada. Si se responde que sí, vacía los datos del contribuyente anterior (RUC, credenciales, certificado y el historial de comprobantes) dejando un respaldo de la base. Si se responde que no, avisa y sigue — pero conviene estar seguro: emitir con el certificado de otro contribuyente es facturar a su nombre.

## Compilar el ejecutable

Solo hace falta al cambiar el código del instalador:

```
python -m pip install pyinstaller
python construir.py
```

Deja `FacturadorSetup.exe` en esta carpeta. No se versiona: se publica como release en GitHub.

## Los archivos

| Archivo | Qué es |
|---|---|
| `instalador.py` | El menú y el flujo de cada operación |
| `sistema.py` | Prerrequisitos, winget, PM2, descarga del SFS |
| `contribuyente.py` | Validaciones, carga en el SFS, `.env`, limpieza |
| `chequeos.py` | El diagnóstico de la opción 3 |
| `construir.py` | Compila el `.exe` |

`sfs.config.js`, en la raíz del proyecto, **lo genera la opción 2** con las rutas de cada PC. No se versiona: tenerlo en el repositorio hacía que una instalación nueva heredara las rutas de otra máquina y PM2 no arrancara.

Ahí también se le ponen los topes de memoria al SFS (`-Xmx512m` y compañía). Sin ellos el JVM se toma hasta un cuarto de la RAM del equipo, y en una PC de 8 GB que además corre otras cosas se queda sin memoria para arrancar: levanta, sirve un rato y se muere sin escribir nada en su log. Con los topes usa 270 MB en vez de 850 MB.
