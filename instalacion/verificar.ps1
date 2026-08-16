<#
.SYNOPSIS
    Diagnostica una instalacion del Facturador: entorno, configuracion y estado.

.DESCRIPTION
    Contesta la pregunta que se hace siempre cuando algo no anda: "por que no esta
    emitiendo?". Revisa cada pieza por separado y dice cual falla, en vez de dejar
    que haya que deducirlo de los logs.

    No modifica nada: es de solo lectura.

.PARAMETER ConCertificado
    Ademas pide la contrasena del certificado para verificar que no este vencido y
    que su RUC coincida con el del emisor. Sin esto solo se comprueba que exista,
    porque un .p12 no se puede leer sin su clave.

.EXAMPLE
    .\verificar.ps1
    .\verificar.ps1 -ConCertificado
#>
[CmdletBinding()]
param(
    [switch]$ConCertificado
)

$ErrorActionPreference = "Continue"
$script:Fallas = 0
$script:Avisos = 0

# --- salida ----------------------------------------------------------------

function Titulo($texto) {
    Write-Host ""
    Write-Host $texto -ForegroundColor Cyan
    Write-Host ("-" * $texto.Length) -ForegroundColor DarkGray
}

function Ok($texto)     { Write-Host "  [ OK ] $texto" -ForegroundColor Green }
function Falla($texto)  { Write-Host "  [FALLA] $texto" -ForegroundColor Red;    $script:Fallas++ }
function Aviso($texto)  { Write-Host "  [AVISO] $texto" -ForegroundColor Yellow; $script:Avisos++ }
function Dato($texto)   { Write-Host "         $texto" -ForegroundColor DarkGray }

# --- utilidades ------------------------------------------------------------

function Leer-Env {
    <#
    El .env como tabla. Se ignoran comentarios y lineas sueltas.
    Devuelve $null si el archivo no existe, para distinguirlo de uno vacio.
    #>
    $ruta = Join-Path $PSScriptRoot "..\.env"
    if (-not (Test-Path $ruta)) { return $null }
    $tabla = @{}
    foreach ($linea in Get-Content $ruta -Encoding UTF8) {
        $l = $linea.Trim()
        if ($l -eq "" -or $l.StartsWith("#") -or -not $l.Contains("=")) { continue }
        $i = $l.IndexOf("=")
        $tabla[$l.Substring(0, $i).Trim()] = $l.Substring($i + 1).Trim()
    }
    # El coma evita que PowerShell desarme la tabla al devolverla.
    return ,$tabla
}

function Version-De($comando, $argumento) {
    # OJO: el parametro NO puede llamarse $args — es una variable automatica de
    # PowerShell y no se recibiria el valor. Con el argumento vacio, "python" abre
    # una consola interactiva y el script queda colgado esperando entrada.
    if (-not (Get-Command $comando -ErrorAction SilentlyContinue)) { return $null }
    try {
        $salida = & $comando $argumento 2>&1 | Out-String
        return ($salida.Trim() -split "`r?`n" | Select-Object -First 1)
    } catch { return $null }
}

# ===========================================================================
Write-Host ""
Write-Host "  VERIFICACION DEL FACTURADOR SUNAT" -ForegroundColor White
Write-Host "  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor DarkGray

# --- 1. Prerrequisitos -----------------------------------------------------
Titulo "1. Prerrequisitos"

$py = Version-De "python" "--version"
if ($py -match "Python (\d+)\.(\d+)") {
    if ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 10)) {
        Ok "$py"
    } else {
        Falla "$py - se necesita 3.10 o superior"
    }
} else {
    Falla "Python no esta instalado o no esta en el PATH"
}

# El SFS es una aplicacion Java: sin JRE no arranca.
# java escribe su version en stderr, y PowerShell le antepone "java.exe : " al
# mezclarlo con stdout; se recorta para que la linea quede legible.
$java = Version-De "java" "-version"
if ($java) {
    Ok "Java: $($java -replace '^java\.exe\s*:\s*', '')"
} else {
    Falla "Java no esta instalado (lo necesita el SFS)"
}

$node = Version-De "node" "--version"
if ($node) { Ok "Node: $node" } else { Falla "Node no esta instalado (lo necesita PM2)" }

$pm2 = Version-De "pm2" "--version"
if ($pm2) { Ok "PM2: $pm2" } else { Falla "PM2 no esta instalado" }

foreach ($paquete in @("psycopg2", "dotenv", "watchdog")) {
    python -c "import $paquete" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Ok "modulo de Python '$paquete'"
    } else {
        Falla "falta el modulo de Python '$paquete' (pip install -r requirements.txt)"
    }
}

# --- 2. Configuracion del daemon -------------------------------------------
Titulo "2. Configuracion del daemon (.env)"

$conf = Leer-Env
if ($null -eq $conf) {
    Falla "no existe el archivo .env"
} else {
    # Sin estas dos el daemon no arranca; el resto tiene valores por defecto.
    foreach ($clave in @("DATABASE_URL", "EMISOR_RUC")) {
        if ($conf.ContainsKey($clave) -and $conf[$clave]) {
            if ($clave -eq "DATABASE_URL") {
                Ok "$clave definida"   # nunca se imprime: lleva la contrasena
            } else {
                Ok "$clave = $($conf[$clave])"
            }
        } else {
            Falla "falta $clave en el .env"
        }
    }
    # Sin credenciales SOL no se cierra ningun resumen diario: su CDR llega
    # detras de un ticket y no hay otra forma de traerlo.
    if ($conf["SOL_USUARIO"] -and $conf["SOL_CLAVE"]) {
        Ok "credenciales SOL configuradas (usuario $($conf['SOL_USUARIO']))"
    } else {
        Falla "faltan SOL_USUARIO / SOL_CLAVE: sin eso NO se cierra ningun resumen de boletas"
    }
}

# --- Rutas de la instalacion ------------------------------------------------
# Se resuelven una sola vez: el .env manda, y si no esta se usa la ubicacion
# habitual del SFS.
$bdSfs   = if ($conf -and $conf["SFS_BD_PATH"])   { $conf["SFS_BD_PATH"] }   else { "C:\SFS_v-2.1\bd\BDFacturador.db" }
$urlSfs  = if ($conf -and $conf["SFS_BASE_URL"])  { $conf["SFS_BASE_URL"] }  else { "http://localhost:9000" }
$dataDir = if ($conf -and $conf["SFS_DATA_DIR"])  { $conf["SFS_DATA_DIR"] }  else { "C:\SFS_v-2.1\sunat_archivos\sfs\DATA" }

# --- Chequeos del lado Python ----------------------------------------------
# Una sola pasada por _chequeos.py, que habla con las dos bases y con PM2. Vive
# aparte porque incrustar Python en un here-string de PowerShell rompe las
# comillas, y porque ConvertFrom-Json no puede con la salida de 'pm2 jlist'.
$chk = @{}
$scriptPy = Join-Path $PSScriptRoot "_chequeos.py"
if (-not (Test-Path $scriptPy)) {
    Falla "falta $scriptPy - no se pueden hacer los chequeos de base de datos ni de PM2"
} else {
    Push-Location (Join-Path $PSScriptRoot "..")
    $lineas = python $scriptPy $bdSfs 2>&1
    Pop-Location
    foreach ($l in $lineas) {
        $t = "$l".Trim()
        # Los digitos importan: hay claves como 'pm2_facturador'.
        if ($t -match "^([a-zA-Z_][a-zA-Z0-9_]*)=(.*)$") { $chk[$Matches[1]] = $Matches[2] }
    }
}

# --- 3. Base de datos de la aplicacion -------------------------------------
Titulo "3. Base de datos de la aplicacion"

if (-not ($conf -and $conf["DATABASE_URL"])) {
    Aviso "sin DATABASE_URL no se puede probar la conexion"
} elseif ($chk["bd_ok"] -eq "1") {
    Ok "conexion establecida"
    Dato "comprobantes sin enviar: $($chk['bd_pendientes'])"
    if ([double]$chk["bd_desfase"] -ne 0) {
        Ok "el reloj de la BD se corrige en $($chk['bd_desfase']) h para declarar a SUNAT"
    } else {
        Ok "la BD ya esta en hora local (sin correccion)"
    }
} else {
    Falla "no se pudo conectar: $($chk['bd_error'])"
}

# --- 4. SFS ----------------------------------------------------------------
Titulo "4. Facturador SUNAT (SFS)"

try {
    $r = Invoke-WebRequest -Uri "$urlSfs/" -TimeoutSec 8 -UseBasicParsing
    Ok "responde en $urlSfs (HTTP $($r.StatusCode))"
} catch {
    Falla "no responde en $urlSfs - el daemon no puede entregarle nada"
}

if (-not (Test-Path $bdSfs)) {
    Falla "no se encontro la base del SFS en $bdSfs"
} elseif ($chk["sfs_error"]) {
    Falla "no se pudo leer la configuracion del SFS: $($chk['sfs_error'])"
} else {
    Ok "base del SFS encontrada"
    # Los datos del emisor viven en su tabla PARAMETRO; sin ellos SUNAT observa
    # el comprobante (p.ej. codigo 4092 por nombre comercial vacio).
    foreach ($campo in @(@("NUMRUC","RUC"), @("RAZON","razon social"), @("NOMCERT","certificado"),
                         @("UBIGEO","ubigeo"), @("NOMCOM","nombre comercial"), @("USUSOL","usuario SOL"))) {
        $valor = $chk["sfs_$($campo[0])"]
        if ($valor) {
            Ok "$($campo[1]): $valor"
        } else {
            Falla "el SFS no tiene configurado: $($campo[1])"
        }
    }

    # El RUC del .env y el del SFS tienen que ser el mismo contribuyente.
    $rucSfs = $chk["sfs_NUMRUC"]
    if ($conf -and $conf["EMISOR_RUC"] -and $rucSfs -and $conf["EMISOR_RUC"] -ne $rucSfs) {
        Falla "el RUC del .env ($($conf['EMISOR_RUC'])) NO coincide con el del SFS ($rucSfs)"
    }
}

# --- 5. Ambiente: produccion o beta ----------------------------------------
Titulo "5. Ambiente de SUNAT"

$constantes = Join-Path (Split-Path $dataDir -Parent) "VALI\constantes.properties"

if (Test-Path $constantes) {
    $activa = Get-Content $constantes -Encoding UTF8 | Where-Object { $_ -match "^\s*RUTA_SERV_CDP\s*=" } | Select-Object -First 1
    if ($activa -match "e-beta") {
        Aviso "BETA - los comprobantes NO tienen validez fiscal"
        Dato "Ningun comprobante real debe emitirse en este estado."
    } elseif ($activa -match "e-factura") {
        Ok "PRODUCCION - los comprobantes tienen validez fiscal"
    } else {
        Falla "no se pudo determinar el ambiente en constantes.properties"
    }
    Dato ($activa -replace "^\s*RUTA_SERV_CDP\s*=\s*", "")
} else {
    Falla "no se encontro constantes.properties en $constantes"
}

# --- 6. Certificado digital ------------------------------------------------
Titulo "6. Certificado digital"

$certDir = Join-Path (Split-Path $dataDir -Parent) "CERT"
$certs = @()
if (Test-Path $certDir) { $certs = @(Get-ChildItem $certDir -Filter "*.p12" -ErrorAction SilentlyContinue) }

if ($certs.Count -eq 0) {
    Falla "no hay ningun certificado .p12 en $certDir"
} else {
    foreach ($c in $certs) { Ok "$($c.Name) ($([math]::Round($c.Length/1KB,1)) KB)" }

    if ($ConCertificado) {
        # Un .p12 no se puede abrir sin su clave: por eso este chequeo es opcional.
        $clave = Read-Host "  Contrasena del certificado" -AsSecureString
        try {
            $x = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2(
                    $certs[0].FullName, $clave,
                    [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::EphemeralKeySet)
            $dias = [int]($x.NotAfter - (Get-Date)).TotalDays
            if ($dias -lt 0) {
                Falla "VENCIDO el $($x.NotAfter.ToString('yyyy-MM-dd')) - no puede firmar"
            } elseif ($dias -lt 30) {
                Aviso "vence en $dias dias ($($x.NotAfter.ToString('yyyy-MM-dd'))) - conviene renovarlo"
            } else {
                Ok "vigente hasta $($x.NotAfter.ToString('yyyy-MM-dd')) ($dias dias)"
            }
            # El RUC va dentro del subject; si es de otro contribuyente, SUNAT
            # rechaza con un error que no dice que el problema es el certificado.
            $rucEmisor = $chk["sfs_NUMRUC"]
            if (-not $rucEmisor -and $conf) { $rucEmisor = $conf["EMISOR_RUC"] }
            if ($rucEmisor) {
                if ($x.Subject -match $rucEmisor) {
                    Ok "el certificado corresponde al RUC $rucEmisor"
                } else {
                    Falla "el certificado NO corresponde al RUC $rucEmisor"
                    Dato "titular: $($x.Subject)"
                }
            } else {
                Aviso "no se pudo comparar el RUC: falta el del emisor"
            }
        } catch {
            Falla "no se pudo abrir el certificado (contrasena incorrecta?)"
        }
    } else {
        Dato "Usar -ConCertificado para verificar vencimiento y titular."
    }
}

# --- 7. Procesos -----------------------------------------------------------
Titulo "7. Procesos (PM2)"

if ($chk["pm2_error"]) {
    Falla "no se pudo leer la lista de procesos de PM2: $($chk['pm2_error'])"
} elseif ($chk["pm2_ok"] -ne "1") {
    Falla "PM2 no respondio"
} else {
    foreach ($nombre in @("sfs", "facturador")) {
        $datos = $chk["pm2_$nombre"]
        if (-not $datos) {
            Falla "'$nombre' no esta en PM2 (pm2 start sfs.config.js)"
            continue
        }
        $campos = $datos -split "\|"
        if ($campos[0] -ne "online") {
            Falla "'$nombre' esta en estado '$($campos[0])'"
            continue
        }
        # pm_uptime es el instante de arranque en milisegundos desde 1970 (UTC).
        $desde = [DateTimeOffset]::FromUnixTimeMilliseconds([long]$campos[2]).LocalDateTime
        $horas = [math]::Round(((Get-Date) - $desde).TotalHours, 1)
        $reinicios = [int]$campos[1]
        Ok "'$nombre' online (${horas}h, $reinicios reinicios)"
        # Reinicios repetidos suelen ser un crash en bucle, no una parada normal.
        if ($reinicios -gt 20) {
            Aviso "'$nombre' se reinicio $reinicios veces: revisar 'pm2 logs $nombre'"
        }
    }
}

# Sin esto, al reiniciar la PC no arranca nada solo.
$registro = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -ErrorAction SilentlyContinue
if ($registro -and ($registro.PSObject.Properties.Name -match "pm2")) {
    Ok "PM2 arranca automaticamente con Windows"
} else {
    Aviso "PM2 no esta registrado para arrancar con Windows (pm2-startup install)"
}

# --- 8. Trabajo pendiente --------------------------------------------------
Titulo "8. Estado del trabajo"

if (-not $chk.ContainsKey("sfs_estados")) {
    Aviso "no se pudo leer la bandeja del SFS"
} elseif (-not $chk["sfs_estados"]) {
    # Instalacion nueva: todavia no se emitio nada. No es un problema.
    Ok "la bandeja del SFS esta vacia (aun no se emitio ningun comprobante)"
} else {
    $estado = $chk["sfs_estados"]
    $nombres = @{
        "01"="por generar XML"; "02"="XML generado"; "03"="aceptado";
        "04"="aceptado c/obs";  "05"="anulado";      "06"="con errores";
        "07"="XML por validar"; "08"="enviado, por procesar"; "09"="procesando";
        "10"="rechazado";       "11"="CDR descargado"; "12"="CDR descargado c/obs"
    }
    $bloqueados = 0
    foreach ($par in ("$estado" -split ";")) {
        $kv = $par -split ":"
        if ($kv.Count -ne 2) { continue }
        $etiqueta = $nombres[$kv[0]]
        if (-not $etiqueta) { $etiqueta = $kv[0] }
        # 05/06/10 no se resuelven solos: necesitan que alguien los mire.
        if ($kv[0] -in @("05","06","10")) {
            Aviso "$($kv[1]) documento(s) BLOQUEADOS: $etiqueta"
            $bloqueados += [int]$kv[1]
        } else {
            Dato "$($kv[1]) documento(s): $etiqueta"
        }
    }
    if ($bloqueados -eq 0) { Ok "ningun documento bloqueado" }
}

if (Test-Path $dataDir) {
    $viejos = @(Get-ChildItem $dataDir -File -ErrorAction SilentlyContinue |
                Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-2) })
    if ($viejos.Count -gt 0) {
        Aviso "$($viejos.Count) archivo(s) en DATA de hace mas de 2 dias: quedaron sin cerrar"
    } else {
        Ok "DATA sin archivos atascados"
    }
}

# --- Resumen ---------------------------------------------------------------
Write-Host ""
Write-Host ("=" * 60) -ForegroundColor DarkGray
if ($script:Fallas -eq 0 -and $script:Avisos -eq 0) {
    Write-Host "  TODO CORRECTO" -ForegroundColor Green
} elseif ($script:Fallas -eq 0) {
    Write-Host "  FUNCIONA, con $($script:Avisos) aviso(s) para revisar" -ForegroundColor Yellow
} else {
    Write-Host "  $($script:Fallas) FALLA(S) y $($script:Avisos) aviso(s)" -ForegroundColor Red
    Write-Host "  El facturador NO va a emitir correctamente hasta corregirlas." -ForegroundColor Red
}
Write-Host ("=" * 60) -ForegroundColor DarkGray
Write-Host ""

exit $(if ($script:Fallas -gt 0) { 1 } else { 0 })
