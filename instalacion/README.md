# Instalación en un cliente nuevo

Tres scripts, uno por problema distinto. Se corren desde esta carpeta, en PowerShell.

| Script | Qué hace | Cuándo |
|---|---|---|
| `instalar.ps1` | Prerrequisitos, dependencias, PM2 y descarga del SFS | Una vez por PC |
| `configurar.ps1` | Carga los datos del cliente y deja todo andando | Al instalar, y cuando cambia una clave o vence el certificado |
| `verificar.ps1` | Diagnostica una instalación existente | Cuando "no está emitiendo" |

Separar configurar de instalar importa: cuando a un cliente le vence el certificado —que pasa— no hay que reinstalar nada, se corre un script de 30 segundos.

## Antes de empezar

Hay que tener a mano, del cliente:

- RUC, razón social y dirección fiscal **con ubigeo** (6 dígitos)
- Usuario y clave **SOL secundarios** (no los principales)
- El **certificado digital** `.p12` o `.pfx` y su contraseña
- La `DATABASE_URL` de la aplicación

Y en la PC: Python 3.10+, Java 8+ y Node.js. `instalar.ps1` avisa si falta alguno, con el enlace de descarga; no los instala solo a propósito — bajar runtimes sin que el operador lo sepa es meterse donde el instalador no debería decidir.

## El procedimiento

```powershell
.\instalar.ps1        # prepara el entorno (descarga ~90 MB de SUNAT)
.\configurar.ps1      # pide los datos del cliente; queda en BETA
```

Después de configurar, **la instalación queda apuntando a beta**. Eso es a propósito:

1. Emitir un comprobante de prueba y confirmar que SUNAT lo acepte
2. Borrar los comprobantes de prueba de la base de la aplicación
3. Recién entonces: `.\configurar.ps1 -Produccion`

**Por qué no instalar directo en producción.** Si algo está mal configurado y un comprobante real sale contra beta, el daemon lo marca como `enviado=true` en la base de la aplicación pero ante SUNAT no existe — y después nada delata la diferencia. Es el peor error posible de este sistema, y arrancar en beta lo vuelve imposible.

## Lo que el instalador no hace, y por qué

**No trae el certificado.** Es la clave privada con la que se firman los comprobantes: copiarlo entre clientes sería darle a uno la capacidad de facturar a nombre de otro. Lo aporta cada cliente al instalar.

**No inventa las claves SOL.** Se le pasan al SFS para que las encripte él, igual que si se tipearan en su pantalla. Replicar su algoritmo sería frágil y se rompería con cualquier actualización suya.

**No prende el temporizador del SFS.** Sus jobs internos de generar y enviar harían por su cuenta lo mismo que el daemon hace por REST, compitiendo por los mismos documentos.

## Reutilizar una PC que ya tenía otro cliente

`instalar.ps1` lo detecta y pregunta antes de borrar nada. Si se responde que sí, vacía los datos del contribuyente anterior (RUC, credenciales, certificado y el historial de comprobantes) dejando un respaldo de la base. Si se responde que no, avisa y sigue — pero conviene estar seguro: emitir con el certificado de otro contribuyente es facturar a su nombre.

## Versión del SFS

Por defecto se instala la **2.1**. SUNAT publica hasta la 2.4 en
`http://www2.sunat.gob.pe/facturador/SFS_v-<versión>.zip` (descarga pública, sin clave SOL; el nombre lleva un guión después de la `v`).

Se fija la 2.1 porque es la única contra la que se verificó el formato de los archivos planos que genera el daemon, incluido el del resumen diario de boletas. Actualizar es una decisión aparte: hay que volver a probar la emisión de cada tipo de comprobante antes de darla por buena.

```powershell
.\instalar.ps1 -VersionSFS "2.4"   # solo después de probarlo
```

## Diagnóstico

```powershell
.\verificar.ps1                 # revisa todo
.\verificar.ps1 -ConCertificado # además: vencimiento y titular del certificado
```

Revisa prerrequisitos, `.env`, la base de la aplicación, la configuración del SFS, **si apunta a producción o beta**, el certificado, los procesos de PM2 y el trabajo pendiente. No modifica nada y no escribe en `facturador.log`, que es donde se investiga qué pasó con un comprobante real.

Devuelve código de salida distinto de cero si encuentra fallas, así que sirve para monitoreo automático.
