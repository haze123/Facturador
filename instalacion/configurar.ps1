<#
.SYNOPSIS
    Carga los datos de un contribuyente en el SFS y en el daemon.

.DESCRIPTION
    Deja una instalacion lista para emitir: configura el SFS (emisor, credenciales
    SOL, certificado), escribe el .env del daemon y registra ambos procesos en PM2.

    Se puede volver a correr cuando cambia algo del cliente —un certificado que
    vence, una clave SOL nueva— sin reinstalar nada.

    QUEDA APUNTANDO A BETA a proposito. Pasar a produccion es un paso aparte y
    deliberado, despues de emitir un comprobante de prueba y verlo aceptado: un
    comprobante real emitido contra beta se marca como enviado en la base de la
    aplicacion sin tener validez fiscal, y nada delata despues la diferencia.

.PARAMETER Produccion
    Configura apuntando a produccion en vez de beta. Usar solo cuando la
    instalacion ya fue probada.

.EXAMPLE
    .\configurar.ps1
    .\configurar.ps1 -Produccion
#>
[CmdletBinding()]
param(
    [switch]$Produccion,
    [string]$RutaSFS = "C:\SFS_v-2.1"
)

$ErrorActionPreference = "Stop"
$raiz = Split-Path $PSScriptRoot -Parent

function Titulo($t) {
    Write-Host ""
    Write-Host $t -ForegroundColor Cyan
    Write-Host ("-" * $t.Length) -ForegroundColor DarkGray
}
function Ok($t)    { Write-Host "  [ OK ] $t" -ForegroundColor Green }
function Error2($t){ Write-Host "  [ERROR] $t" -ForegroundColor Red }
function Nota($t)  { Write-Host "         $t" -ForegroundColor DarkGray }

function Abortar($mensaje) {
    Write-Host ""
    Error2 $mensaje
    Write-Host "  Configuracion interrumpida: no se cambio nada a medias." -ForegroundColor Red
    Write-Host ""
    exit 1
}

# --- validaciones ----------------------------------------------------------

function Test-Ruc([string]$ruc) {
    <#
    Digito verificador del RUC (modulo 11). Un RUC mal tipeado se descubriria
    recien cuando SUNAT rechaza el primer comprobante, y para entonces ya esta
    todo configurado con el numero equivocado.
    #>
    if ($ruc -notmatch "^\d{11}$") { return $false }
    $factores = 5,4,3,2,7,6,5,4,3,2
    $suma = 0
    for ($i = 0; $i -lt 10; $i++) {
        $suma += [int]::Parse($ruc[$i]) * $factores[$i]
    }
    $resto = $suma % 11
    $dv = 11 - $resto
    if ($dv -eq 10) { $dv = 0 }
    if ($dv -eq 11) { $dv = 1 }
    return ($dv -eq [int]::Parse($ruc[10]))
}

function Pedir($etiqueta, $valorActual, [switch]$Obligatorio) {
    <# Pregunta mostrando el valor actual entre corchetes; Enter lo conserva. #>
    while ($true) {
        $sufijo = if ($valorActual) { " [$valorActual]" } else { "" }
        $r = Read-Host "  $etiqueta$sufijo"
        if (-not $r -and $valorActual) { return $valorActual }
        if ($r) { return $r.Trim() }
        if (-not $Obligatorio) { return "" }
        Write-Host "    (este dato es obligatorio)" -ForegroundColor Yellow
    }
}

function Texto-De([System.Security.SecureString]$seguro) {
    <# El texto plano se necesita para hablar con el SFS; se libera enseguida. #>
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($seguro)
    try   { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

function Post-SFS($url, $ruta, $cuerpo) {
    try {
        $r = Invoke-RestMethod -Uri "$url/api/$ruta" -Method Post `
                -ContentType "application/json; charset=utf-8" `
                -Body ($cuerpo | ConvertTo-Json -Compress) -TimeoutSec 60
        return $r
    } catch {
        return @{ validacion = "FALLO"; mensaje = $_.Exception.Message }
    }
}

# ===========================================================================
Write-Host ""
Write-Host "  CONFIGURACION DEL FACTURADOR SUNAT" -ForegroundColor White
$ambiente = if ($Produccion) { "PRODUCCION" } else { "BETA (pruebas)" }
$color    = if ($Produccion) { "Red" } else { "Yellow" }
Write-Host "  Ambiente: $ambiente" -ForegroundColor $color

if ($Produccion) {
    Write-Host ""
    Write-Host "  Los comprobantes que se emitan tendran validez fiscal." -ForegroundColor Red
    $c = Read-Host "  Escribir SI para continuar"
    if ($c -ne "SI") { Abortar "cancelado por el operador" }
}

# --- 1. Comprobaciones previas ---------------------------------------------
Titulo "1. Comprobaciones previas"

foreach ($req in @(@("python","Python"), @("java","Java"), @("node","Node"), @("pm2","PM2"))) {
    if (Get-Command $req[0] -ErrorAction SilentlyContinue) {
        Ok "$($req[1]) disponible"
    } else {
        Abortar "falta $($req[1]). Correr primero instalar.ps1"
    }
}

if (-not (Test-Path (Join-Path $RutaSFS "facturadorApp-2.1.jar"))) {
    Abortar "no se encontro el SFS en $RutaSFS. Correr primero instalar.ps1"
}
Ok "SFS encontrado en $RutaSFS"

# --- 2. Datos del contribuyente --------------------------------------------
Titulo "2. Datos del contribuyente"
Nota "Enter conserva el valor entre corchetes."

# Si ya hay una configuracion, se ofrece como valor por defecto.
$previo = @{}
$bdSfs = Join-Path $RutaSFS "bd\BDFacturador.db"
if (Test-Path $bdSfs) {
    $salida = python (Join-Path $PSScriptRoot "_chequeos.py") $bdSfs 2>&1
    foreach ($l in $salida) {
        if ("$l".Trim() -match "^sfs_([A-Z]+)=(.*)$") { $previo[$Matches[1]] = $Matches[2] }
    }
}

while ($true) {
    $ruc = Pedir "RUC (11 digitos)" $previo["NUMRUC"] -Obligatorio
    if (Test-Ruc $ruc) { break }
    Error2 "el RUC '$ruc' no es valido (no pasa el digito verificador)"
}
Ok "RUC $ruc"

$razon   = Pedir "Razon social"            $previo["RAZON"]  -Obligatorio
$comercial = Pedir "Nombre comercial"      $previo["NOMCOM"]
if (-not $comercial) { $comercial = $razon }   # vacio -> SUNAT observa con 4092
$usuarioSol = Pedir "Usuario SOL secundario" $previo["USUSOL"] -Obligatorio

$claveSolSeg = Read-Host "  Clave SOL" -AsSecureString
if ($claveSolSeg.Length -eq 0) { Abortar "la clave SOL es obligatoria" }

Titulo "3. Direccion fiscal"
$ubigeo = Pedir "Ubigeo (6 digitos)" $previo["UBIGEO"] -Obligatorio
$direccion = Pedir "Direccion" "" -Obligatorio
$departamento = Pedir "Departamento" "LIMA" -Obligatorio
$provincia = Pedir "Provincia" "LIMA" -Obligatorio
$distrito = Pedir "Distrito" "" -Obligatorio
$urbanizacion = Pedir "Urbanizacion (opcional)" ""

Titulo "4. Certificado digital"
Nota "El .p12 o .pfx que emitio la entidad certificadora para este RUC."
while ($true) {
    $rutaCert = Pedir "Ruta del archivo" "" -Obligatorio
    $rutaCert = $rutaCert.Trim('"')
    if (Test-Path $rutaCert) { break }
    Error2 "no existe el archivo '$rutaCert'"
}
$claveCertSeg = Read-Host "  Contrasena del certificado" -AsSecureString
if ($claveCertSeg.Length -eq 0) { Abortar "la contrasena del certificado es obligatoria" }

# Se valida antes de tocar el SFS: si el certificado es de otro contribuyente,
# SUNAT rechaza con un error que no menciona al certificado.
try {
    $x509 = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
                $rutaCert, $claveCertSeg,
                [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
} catch {
    Abortar "no se pudo abrir el certificado (contrasena incorrecta?)"
}
if ($x509.NotAfter -lt (Get-Date)) {
    Abortar "el certificado vencio el $($x509.NotAfter.ToString('yyyy-MM-dd'))"
}
if ($x509.Subject -notmatch $ruc) {
    Error2 "el certificado NO corresponde al RUC $ruc"
    Nota "titular: $($x509.Subject)"
    Abortar "certificado de otro contribuyente"
}
Ok "certificado valido hasta $($x509.NotAfter.ToString('yyyy-MM-dd')), RUC $ruc"

Titulo "5. Base de datos de la aplicacion"
$dbUrl = Pedir "DATABASE_URL" "" -Obligatorio
$env:PGCONNECT_TIMEOUT = "10"
$prueba = python -c "import sys,psycopg2,urllib.parse as u; p=u.urlsplit(sys.argv[1]); q=u.parse_qs(p.query); e=(q.get('schema') or ['public'])[0]; c=psycopg2.connect(u.urlunsplit((p.scheme,p.netloc,p.path,'','')), connect_timeout=10, options=f'-c search_path={e}'); c.close(); print('OK')" $dbUrl 2>&1 | Select-Object -Last 1
if ("$prueba".Trim() -ne "OK") {
    Error2 "no se pudo conectar a la base"
    Nota "$prueba"
    Abortar "revisar la DATABASE_URL"
}
Ok "conexion verificada"

# --- 6. Configurar el SFS ---------------------------------------------------
Titulo "6. Configurando el SFS"

$urlSfs = "http://localhost:9000"
try {
    Invoke-WebRequest -Uri "$urlSfs/" -TimeoutSec 5 -UseBasicParsing | Out-Null
    Ok "el SFS ya esta corriendo"
} catch {
    Nota "levantando el SFS..."
    pm2 start (Join-Path $raiz "sfs.config.js") --only sfs 2>&1 | Out-Null
    $listo = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        try { Invoke-WebRequest -Uri "$urlSfs/" -TimeoutSec 4 -UseBasicParsing | Out-Null; $listo = $true; break } catch {}
    }
    if (-not $listo) { Abortar "el SFS no respondio despues de 60 segundos" }
    Ok "SFS levantado"
}

$claveSol = Texto-De $claveSolSeg
# cmbFuncionamiento='02' y temporizadores vacios: el SFS NO debe correr sus
# propios jobs de generar/enviar, porque haria por su cuenta lo mismo que el
# daemon hace por REST y competirian por los mismos documentos.
$r = Post-SFS $urlSfs "GrabarParametro.htm" @{
    txtNumeroRuc          = $ruc
    txtRazonSocial        = $razon
    txtUsuarioSol         = $usuarioSol
    txtClaveSol           = $claveSol
    txtUsuarioSolPrincipal= $usuarioSol
    txtClaveSolPrincipal  = $claveSol
    txtRutaSolucion       = $RutaSFS
    txtClient_id          = ""
    txtClient_secret      = ""
    cmbFuncionamiento     = "02"
    cmbTiempoGenera       = ""
    cmbTiempoEnvia        = ""
}
$claveSol = $null
if ($r.validacion -ne "EXITO") { Abortar "el SFS rechazo los datos del emisor: $($r.mensaje)" }
Ok "emisor y credenciales SOL cargados (el SFS encripta las claves)"

$r = Post-SFS $urlSfs "GrabarOtrosParametros.htm" @{
    txtNombreComercial = $comercial
    txtUbigeo          = $ubigeo
    txtDireccion       = $direccion
    txtDepartamento    = $departamento
    txtProvincia       = $provincia
    txtDistrito        = $distrito
    txtUrbanizacion    = $urbanizacion
}
if ($r.validacion -ne "EXITO") { Abortar "el SFS rechazo la direccion: $($r.mensaje)" }
Ok "direccion fiscal cargada"

$claveCert = Texto-De $claveCertSeg
$r = Post-SFS $urlSfs "ImportarCertificado.htm" @{
    nombreCertificado = $rutaCert
    passPrivateKey    = $claveCert
}
$claveCert = $null
if ($r.validacion -ne "EXITO") { Abortar "el SFS rechazo el certificado: $($r.mensaje)" }
Ok "certificado importado"

# --- 7. Ambiente de SUNAT ---------------------------------------------------
Titulo "7. Ambiente de SUNAT"

$constantes = Join-Path $RutaSFS "sunat_archivos\sfs\VALI\constantes.properties"
if (-not (Test-Path $constantes)) { Abortar "no se encontro $constantes" }

$destino = if ($Produccion) {
    "https://e-factura.sunat.gob.pe/ol-ti-itcpfegem/billService"
} else {
    "https://e-beta.sunat.gob.pe/ol-ti-itcpfegem-beta/billService"
}
# Se comentan TODAS las variantes de RUTA_SERV_CDP y se deja activa una sola:
# el SFS toma la primera sin comentar, asi que dos activas serian ambiguas.
$lineas = Get-Content $constantes -Encoding UTF8 | ForEach-Object {
    if ($_ -match "^\s*RUTA_SERV_CDP\s*=") { "#" + $_.TrimStart() } else { $_ }
}
$lineas = $lineas -replace "^#RUTA_SERV_CDP=$([regex]::Escape($destino))$", "RUTA_SERV_CDP=$destino"
if (-not ($lineas -match "^RUTA_SERV_CDP=")) {
    # La URL no figuraba comentada en el archivo: se agrega.
    $lineas += "RUTA_SERV_CDP=$destino"
}
# Sin BOM a proposito: 'Set-Content -Encoding UTF8' de PowerShell 5.1 lo agrega, y
# ese caracter invisible al inicio del archivo deja la primera propiedad con el
# nombre corrupto para quien lo lea (seria "﻿RUTA_WS_EPT" en vez de "RUTA_WS_EPT").
[System.IO.File]::WriteAllLines($constantes, $lineas, (New-Object System.Text.UTF8Encoding($false)))
Ok "apuntando a $ambiente"
Nota $destino

# --- 8. .env del daemon -----------------------------------------------------
Titulo "8. Configuracion del daemon"

$claveSolPlano = Texto-De $claveSolSeg
$envPath = Join-Path $raiz ".env"
$contenido = @"
# Generado por configurar.ps1 el $(Get-Date -Format 'yyyy-MM-dd HH:mm')
DATABASE_URL=$dbUrl

SFS_BASE_URL=$urlSfs
SFS_DATA_DIR=$RutaSFS\sunat_archivos\sfs\DATA
SFS_RPTA_DIR=$RutaSFS\sunat_archivos\sfs\RPTA
SFS_BD_PATH=$RutaSFS\bd\BDFacturador.db

EMISOR_RUC=$ruc

INTERVALO_GENERACION_SEG=60
MAX_REINTENTOS_RECHAZO=3

# Necesarias para cerrar los resumenes diarios: su CDR llega detras de un ticket.
SOL_USUARIO=$usuarioSol
SOL_CLAVE=$claveSolPlano
CONSULTA_SUNAT_TRAS_MIN=10

# El desfase con el reloj de la BD se mide solo en cada ciclo.
DESFASE_BD_HORAS=auto
"@
if (Test-Path $envPath) {
    $respaldo = "$envPath.bak-$(Get-Date -Format 'yyyyMMddHHmmss')"
    Copy-Item $envPath $respaldo
    Nota "se respaldo el .env anterior en $(Split-Path $respaldo -Leaf)"
}
# Sin BOM: con el, python-dotenv leeria la primera variable como "﻿DATABASE_URL"
# y el daemon arrancaria diciendo que falta la DATABASE_URL aunque este ahi.
[System.IO.File]::WriteAllText($envPath, $contenido, (New-Object System.Text.UTF8Encoding($false)))
$claveSolPlano = $null

# El .env lleva la clave SOL y la de la base en texto plano: solo su dueno debe leerlo.
$acl = Get-Acl $envPath
$acl.SetAccessRuleProtection($true, $false)
$acl.SetAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    "$env:USERDOMAIN\$env:USERNAME", "FullControl", "Allow")))
Set-Acl $envPath $acl
Ok ".env escrito y restringido al usuario actual"

# --- 9. Procesos ------------------------------------------------------------
Titulo "9. Procesos"

pm2 start (Join-Path $raiz "sfs.config.js") 2>&1 | Out-Null
pm2 save 2>&1 | Out-Null
Ok "SFS y daemon registrados en PM2"

if (-not (Get-Command "pm2-startup" -ErrorAction SilentlyContinue)) {
    npm install -g pm2-windows-startup 2>&1 | Out-Null
}
pm2-startup install 2>&1 | Out-Null
Ok "arranque automatico con Windows"

# --- 10. Verificacion -------------------------------------------------------
Titulo "10. Verificacion"
Nota "Corriendo verificar.ps1..."
Write-Host ""
& (Join-Path $PSScriptRoot "verificar.ps1")
$resultado = $LASTEXITCODE

Write-Host ""
if ($resultado -eq 0 -and -not $Produccion) {
    Write-Host "  LISTO - la instalacion quedo en BETA" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Antes de pasar a produccion:" -ForegroundColor Yellow
    Write-Host "   1. Emitir un comprobante de prueba y confirmar que SUNAT lo acepte"
    Write-Host "   2. Borrar los comprobantes de prueba de la base"
    Write-Host "   3. Correr:  .\configurar.ps1 -Produccion"
} elseif ($resultado -eq 0) {
    Write-Host "  LISTO - la instalacion quedo en PRODUCCION" -ForegroundColor Green
} else {
    Write-Host "  La configuracion se aplico pero la verificacion encontro problemas." -ForegroundColor Yellow
    Write-Host "  Revisar el detalle de arriba antes de emitir." -ForegroundColor Yellow
}
Write-Host ""
exit $resultado
