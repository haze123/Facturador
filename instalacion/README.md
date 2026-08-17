# Instalador

Un solo ejecutable con menú, `FacturadorSetup.exe`, que lleva adentro su propio Python. Por eso corre en una PC recién formateada, donde todavía no hay nada instalado.

```
   1  Instalar el entorno (prerrequisitos, PM2, SFS)
   2  Configurar un cliente
   3  Verificar la instalacion
   4  Pasar a produccion

   5  Actualizar desde el archivo del cliente
```

Del 1 al 4 en el orden en que se hacen al instalar por primera vez; abajo, separado, lo que se usa cuando la instalación ya existe. Con "actualizar" en el medio, quien venía de hacer 1, 2 y 3 leía el 4 como el paso siguiente cuando el que continuaba la secuencia era el de más abajo.

**El procedimiento paso a paso está en el manual en PDF**, que se distribuye aparte del repositorio. Este documento cubre lo demás: por qué el instalador está armado así y qué hacer cuando algo se sale del camino.

Las cuatro opciones están separadas a propósito: cuando a un cliente le vence el certificado —que pasa— no hay que reinstalar nada, se corre solo la opción 2.

## El archivo de cada cliente

Configurar un cliente de cero (opción 2) pide catorce datos. Un mes después, cuando vence el certificado o corrigen el distrito, volver a recorrer todo es absurdo — y encima obliga a retipear la clave SOL, que no tiene nada que ver con lo que cambió.

Para eso está la **opción 5**: un archivo de texto por cliente con sus datos, que el instalador compara contra lo instalado y aplica solo la diferencia.

```
  Distrito             LOS OLIVOS
                       -> SURQUILLO

  1 cambio(s). Aplicar? (si/no):
```

Funciona porque el SFS tiene **tres endpoints independientes** —emisor, dirección y certificado—, así que se llama únicamente al que corresponde. Y de ahí sale lo mejor: **cada clave se pide solo si su endpoint cambió**. Mover el distrito no pide ninguna; renovar el certificado pide la del certificado y nada más.

Si el archivo no existe, la opción 5 lo genera con los datos que la PC ya tiene, listo para editar con el Bloc de notas.

**Las claves se preguntan siempre, aparte del diff**, porque son lo único que la comparación no puede detectar: no están en el archivo y el SFS las guarda cifradas, así que no hay contra qué compararlas. Si un cliente renueva su clave SOL y no cambia ningún otro dato, el diff sale vacío — y sin ese paso no tendría por dónde actualizarla.

```
  Claves
  ------
       No se pueden comparar: el SFS las guarda cifradas y el archivo no
       las lleva. Si alguna cambio, hay que decirlo aca.
    1  Ninguna
    2  La clave SOL
    3  La contrasena del certificado
    4  Las dos
```

**Las claves no van en el archivo.** Hoy la clave SOL y la del certificado se tipean una vez y el SFS las guarda cifradas: nunca tocan el disco en texto plano. Un `.txt` con la clave SOL en la PC del cliente es otra cosa — se copia, se manda por chat, queda en el Escritorio y sobrevive a que echen al empleado que lo tenía. Por eso el archivo trae la conexión a la base **sin contraseña**, y las dos claves del SFS no figuran en ningún campo.

Cambiar de motor tambien entra por acá: se edita `base_datos` en el archivo y la opción 5 lo detecta como un cambio más. Usa la conexión que dice el archivo y pide **solo la contraseña** —lo único que el archivo no puede llevar—, la prueba antes de guardar, y si no conecta deja el `.env` como estaba en vez de dejarlo apuntando a una base a la que el daemon no llega.

Dos detalles que costaron pensar:

- **El certificado se compara por contenido, no por nombre.** Uno renovado suele llamarse igual que el que vence; mirar el nombre diría "sin cambios" justo el día que hay que reemplazarlo.
- **Cambiar el RUC no es un campo más.** Es otro contribuyente: el certificado está atado a él y el historial emitido pertenece al anterior. La opción 5 frena y pide confirmación explícita, y sugiere la opción 2, que sí limpia lo anterior.

## La base de datos de cada cliente

La opción 2 pregunta el motor —PostgreSQL o SQL Server— y pide los datos que correspondan: servidor, puerto, base, y usuario y clave o autenticación de Windows. Con eso arma la `DATABASE_URL`. Se pregunta por partes porque nadie recuerda la sintaxis de memoria y una URL mal escrita falla con un mensaje del driver que no dice cuál es el pedazo equivocado; igual se puede pegar entera, para el que ya la tiene.

La conexión se prueba antes de guardar nada, **con el mismo código que después usa el daemon** (`repositorio/`). Antes esto duplicaba la conexión de psycopg2: solo sabía de PostgreSQL y podía quedar desincronizado del daemon sin que nadie lo notara. Ahora, si el instalador dice que conecta, el daemon conecta.

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
