<#
.SYNOPSIS
    Prepara una PC nueva para correr el Facturador: prerrequisitos y SFS.

.DESCRIPTION
    Deja el entorno instalado pero SIN datos de ningun contribuyente. Los datos
    del cliente los carga despues configurar.ps1.

    Es idempotente: se puede volver a correr sin romper nada. Lo que ya esta
    instalado se detecta y se saltea, asi que tambien sirve para completar una
    instalacion que quedo a medias.

    NO instala el certificado ni ninguna credencial: eso es por cliente y va en
    el paso siguiente.

.PARAMETER RutaSFS
    Donde instalar el SFS. Por defecto C:\SFS_v-2.1.

.PARAMETER VersionSFS
    Version del SFS a descargar. Por defecto 2.1, que es contra la que se
    verifico el formato de los archivos planos que genera el daemon. Cambiarla
    exige volver a probar la emision de cada tipo de comprobante.

.EXAMPLE
    .\instalar.ps1
    .\instalar.ps1 -RutaSFS "D:\SFS" -VersionSFS "2.4"
#>
[CmdletBinding()]
param(
    [string]$RutaSFS = "C:\SFS_v-2.1",
    [string]$VersionSFS = "2.1"
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path $PSScriptRoot -Parent

function Titulo($t) {
    Write-Host ""
    Write-Host $t -ForegroundColor Cyan
    Write-Host ("-" * $t.Length) -ForegroundColor DarkGray
}
function Ok($t)     { Write-Host "  [ OK ] $t" -ForegroundColor Green }
function Paso($t)   { Write-Host "  [ .. ] $t" -ForegroundColor White }
function Aviso($t)  { Write-Host "  [AVISO] $t" -ForegroundColor Yellow }
function Nota($t)   { Write-Host "         $t" -ForegroundColor DarkGray }

function Abortar($mensaje, $comoSeguir) {
    Write-Host ""
    Write-Host "  [ERROR] $mensaje" -ForegroundColor Red
    if ($comoSeguir) { Write-Host "  $comoSeguir" -ForegroundColor Yellow }
    Write-Host ""
    exit 1
}

function Hay($comando) {
    return [bool](Get-Command $comando -ErrorAction SilentlyContinue)
}

function Refrescar-Path {
    <#
    Vuelve a leer el PATH del registro. Un instalador lo modifica para las
    consolas NUEVAS, pero la que esta corriendo conserva el de su arranque: sin
    esto habria que cerrar y reabrir despues de cada instalacion.
    #>
    $maquina = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $usuario = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = ($maquina, $usuario | Where-Object { $_ }) -join ";"
}

function Instalar-Con-Winget($id, $nombre) {
    <# Devuelve $true si quedo instalado y utilizable. #>
    Paso "instalando $nombre..."
    # --silent evita los asistentes; --accept-*-agreements, los prompts legales.
    winget install --id $id --exact --silent --disable-interactivity `
                   --accept-package-agreements --accept-source-agreements 2>&1 | Out-Null
    Refrescar-Path
    return $?
}

# ===========================================================================
Write-Host ""
Write-Host "  INSTALACION DEL FACTURADOR SUNAT" -ForegroundColor White
Write-Host "  Prepara el entorno; los datos del cliente van en configurar.ps1" -ForegroundColor DarkGray

# --- 1. Prerrequisitos del sistema -----------------------------------------
Titulo "1. Prerrequisitos"

# Lo que hace falta, y con que paquete de winget se resuelve cada uno.
$requeridos = @(
    @{ Comando = "python"; Nombre = "Python 3.12"; Id = "Python.Python.3.12"
       Manual  = "https://www.python.org/downloads/"
       # Un Python viejo tampoco sirve: main.py usa sintaxis de 3.10 en adelante.
       Version = { ((python --version 2>&1 | Out-String) -match "Python (\d+)\.(\d+)") -and
                   ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10)) } }
    @{ Comando = "java";   Nombre = "Java 8 (Temurin)"; Id = "EclipseAdoptium.Temurin.8.JRE"
       Manual  = "https://adoptium.net/" }
    @{ Comando = "node";   Nombre = "Node.js LTS"; Id = "OpenJS.NodeJS.LTS"
       Manual  = "https://nodejs.org/" }
)

$hayWinget = Hay "winget"
if (-not $hayWinget) {
    Aviso "winget no esta disponible: lo que falte habra que instalarlo a mano"
    Nota "winget viene con Windows 10 1809+ y Windows 11, como 'Instalador de aplicacion'"
}

$faltan = @()
foreach ($req in $requeridos) {
    $instalado = Hay $req.Comando
    # Estar en el PATH no alcanza si la version es demasiado vieja.
    if ($instalado -and $req.Version -and -not (& $req.Version)) {
        Aviso "$($req.Nombre): la version instalada es anterior a la minima"
        $instalado = $false
    }

    if ($instalado) {
        $detalle = switch ($req.Comando) {
            "python" { (python --version 2>&1 | Out-String).Trim() }
            "java"   { ((java -version 2>&1 | Out-String).Trim() -split "`r?`n")[0] -replace '^java(\.exe)?\s*:\s*', '' }
            "node"   { "Node $(node --version)" }
        }
        Ok $detalle
        continue
    }

    if (-not $hayWinget) {
        $faltan += "$($req.Nombre) -> $($req.Manual)"
        continue
    }

    Instalar-Con-Winget $req.Id $req.Nombre | Out-Null
    if (Hay $req.Comando) {
        Ok "$($req.Nombre) instalado"
    } else {
        # Algunos instaladores dejan el PATH listo recien para la proxima consola.
        Aviso "$($req.Nombre) se instalo pero aun no esta en el PATH de esta consola"
        $faltan += "$($req.Nombre) (reabrir la consola)"
    }
}

if ($faltan.Count -gt 0) {
    Write-Host ""
    Write-Host "  Falta resolver:" -ForegroundColor Red
    foreach ($f in $faltan) { Write-Host "    - $f" -ForegroundColor Red }
    Abortar "no se puede continuar sin los prerrequisitos" `
            "Si se acaban de instalar: cerrar esta consola, abrir una nueva y volver a correr el script."
}

# --- 2. Dependencias de Python ---------------------------------------------
Titulo "2. Dependencias de Python"

$requisitos = Join-Path $raiz "requirements.txt"
if (-not (Test-Path $requisitos)) { Abortar "no se encontro $requisitos" }

Paso "instalando psycopg2, python-dotenv, watchdog..."
$salida = python -m pip install -r $requisitos --disable-pip-version-check 2>&1 | Out-String
foreach ($paquete in @("psycopg2", "dotenv", "watchdog")) {
    python -c "import $paquete" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Ok "$paquete"
    } else {
        Nota ($salida.Trim() -split "`r?`n" | Select-Object -Last 3)
        Abortar "no se pudo instalar '$paquete'"
    }
}

# --- 3. PM2 ------------------------------------------------------------------
Titulo "3. PM2"

if (Hay "pm2") {
    Ok "PM2 ya instalado ($(pm2 --version 2>$null | Select-Object -Last 1))"
} else {
    Paso "instalando PM2..."
    npm install -g pm2 2>&1 | Out-Null
    if (Hay "pm2") { Ok "PM2 instalado" } else { Abortar "no se pudo instalar PM2" }
}

# Windows no tiene init system, asi que 'pm2 startup' no alcanza: hace falta
# este paquete para que PM2 resucite sus procesos al iniciar sesion.
if (Hay "pm2-startup") {
    Ok "pm2-windows-startup ya instalado"
} else {
    Paso "instalando pm2-windows-startup..."
    npm install -g pm2-windows-startup 2>&1 | Out-Null
    if (Hay "pm2-startup") { Ok "pm2-windows-startup instalado" }
    else { Aviso "no se pudo instalar; el facturador no arrancara solo al prender la PC" }
}

# --- 4. SFS ------------------------------------------------------------------
Titulo "4. Facturador SUNAT (SFS $VersionSFS)"

$jar = Join-Path $RutaSFS "facturadorApp-$VersionSFS.jar"
if (Test-Path $jar) {
    Ok "ya instalado en $RutaSFS"
} else {
    # Descarga publica y directa de SUNAT, sin clave SOL. El nombre lleva un
    # guion despues de la 'v' (SFS_v-2.1.zip), no es un error de tipeo.
    $url = "http://www2.sunat.gob.pe/facturador/SFS_v-$VersionSFS.zip"
    $zip = Join-Path $env:TEMP "SFS_v-$VersionSFS.zip"

    Paso "descargando de SUNAT (~90 MB)..."
    Nota $url
    try {
        $anterior = $ProgressPreference
        $ProgressPreference = "SilentlyContinue"   # la barra de progreso lo hace lentisimo
        Invoke-WebRequest -Uri $url -OutFile $zip -TimeoutSec 900 -UseBasicParsing
        $ProgressPreference = $anterior
    } catch {
        Abortar "no se pudo descargar el SFS: $($_.Exception.Message)" `
                "Verificar la conexion, o bajarlo a mano de $url y descomprimirlo en $RutaSFS"
    }

    # Que sea realmente un ZIP y no una pagina de error devuelta con codigo 200.
    $magic = [System.IO.File]::ReadAllBytes($zip)[0..1]
    if ($magic[0] -ne 0x50 -or $magic[1] -ne 0x4B) {
        Remove-Item $zip -Force
        Abortar "lo descargado no es un archivo ZIP" "Puede que la version $VersionSFS ya no exista en SUNAT."
    }
    Ok "descargado ($([math]::Round((Get-Item $zip).Length/1MB,1)) MB)"

    Paso "descomprimiendo en $RutaSFS..."
    $destino = Split-Path $RutaSFS -Parent
    if (-not (Test-Path $destino)) { New-Item -ItemType Directory -Force $destino | Out-Null }
    try {
        Expand-Archive -Path $zip -DestinationPath $destino -Force
    } catch {
        Abortar "no se pudo descomprimir: $($_.Exception.Message)"
    }
    Remove-Item $zip -Force

    if (-not (Test-Path $jar)) {
        Abortar "el ZIP no dejo $jar" "Revisar que se haya descomprimido en $destino"
    }
    Ok "SFS instalado en $RutaSFS"
}

# --- 5. Que quede sin datos de ningun contribuyente -------------------------
Titulo "5. Instalacion limpia"

# Si el SFS viene de otra instalacion (una copia, o una prueba anterior), traeria
# el RUC, las credenciales y hasta el certificado del contribuyente anterior.
# Emitir con eso significaria facturar a nombre de otro.
$bdSfs = Join-Path $RutaSFS "bd\BDFacturador.db"
$ruc = ""
if (Test-Path $bdSfs) {
    # Se reutiliza _chequeos.py, que ya sabe leer la configuracion del SFS.
    $salida = python (Join-Path $PSScriptRoot "_chequeos.py") $bdSfs 2>&1
    foreach ($l in $salida) {
        if ("$l".Trim() -match "^sfs_NUMRUC=(.*)$") { $ruc = $Matches[1].Trim() }
    }
}
$hayDatos = [bool]$ruc

$certDir = Join-Path $RutaSFS "sunat_archivos\sfs\CERT"
$certs = @()
if (Test-Path $certDir) { $certs = @(Get-ChildItem $certDir -Filter "*.p*" -ErrorAction SilentlyContinue) }

if ($hayDatos -or $certs.Count -gt 0) {
    Write-Host ""
    Aviso "esta instalacion ya tiene datos de un contribuyente"
    if ($hayDatos)          { Nota "RUC configurado: $ruc" }
    if ($certs.Count -gt 0) { Nota "certificado(s): $(($certs | ForEach-Object { $_.Name }) -join ', ')" }
    Write-Host ""
    Write-Host "  Si es para OTRO cliente hay que borrarlos: emitir con el certificado" -ForegroundColor Yellow
    Write-Host "  de otro contribuyente es facturar a su nombre." -ForegroundColor Yellow
    $r = Read-Host "  Borrar los datos del contribuyente anterior? (si/no)"
    if ($r -eq "si") {
        foreach ($c in $certs) { Remove-Item $c.FullName -Force }
        if (Test-Path $bdSfs) {
            # Respaldo antes de tocar nada: si el operador se equivoco de PC,
            # esto es lo unico que permite volver atras.
            $respaldo = "$bdSfs.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
            Copy-Item $bdSfs $respaldo
            Nota "respaldo en $(Split-Path $respaldo -Leaf)"
            $res = python (Join-Path $PSScriptRoot "_limpiar_contribuyente.py") $bdSfs 2>&1 |
                   Select-Object -Last 1
            if ("$res" -notmatch "^ok\|") {
                Abortar "no se pudieron borrar los datos anteriores: $res" `
                        "La base quedo intacta y hay un respaldo en $(Split-Path $respaldo -Leaf)"
            }
            $campos, $docs = ("$res" -split "\|")[1,2]
            Nota "$campos parametro(s) vaciados, $docs comprobante(s) del historial borrados"
        }
        Ok "datos del contribuyente anterior eliminados"
    } else {
        Aviso "se conservan; verificar que sean del cliente correcto antes de emitir"
    }
} else {
    Ok "sin datos de ningun contribuyente"
}

# Las carpetas de trabajo tienen que existir antes de emitir.
foreach ($carpeta in @("sunat_archivos\sfs\DATA", "sunat_archivos\sfs\RPTA", "sunat_archivos\sfs\CERT")) {
    $ruta = Join-Path $RutaSFS $carpeta
    if (-not (Test-Path $ruta)) { New-Item -ItemType Directory -Force $ruta | Out-Null }
}
Ok "carpetas de trabajo listas"

# --- 6. Listo ---------------------------------------------------------------
Write-Host ""
Write-Host ("=" * 62) -ForegroundColor DarkGray
Write-Host "  ENTORNO LISTO" -ForegroundColor Green
Write-Host ("=" * 62) -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Falta cargar los datos del cliente. Tener a mano:" -ForegroundColor White
Write-Host "    - RUC, razon social y direccion fiscal (con ubigeo)"
Write-Host "    - Usuario y clave SOL secundarios"
Write-Host "    - El certificado digital (.p12) y su contrasena"
Write-Host "    - La DATABASE_URL de la aplicacion"
Write-Host ""
Write-Host "  Despues correr:  .\configurar.ps1" -ForegroundColor Cyan
Write-Host ""
