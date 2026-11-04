param(
    [string]$PythonVersion = "3.13",
    [string]$VenvPath = ".venv313",
    # Carpeta de una instalacion de Tesseract OCR a incluir en el .exe (por defecto se busca sola).
    [string]$TesseractDir = "",
    # Genera el .exe SIN OCR (la lectura de imagenes de facturas exigira Tesseract instalado en cada PC).
    [switch]$SkipOcr,
    # No adelgaza Tesseract: se incluye tal cual esta instalado (el .exe pesa ~100 MB mas).
    [switch]$NoSlim,
    # Compila el ejecutable de DESARROLLADOR (todas las funciones sin licencia) que SOLO funciona
    # en este PC. Queda en dist_dev\FileSync Pro DEV.exe. Nunca lo distribuyas.
    [switch]$Dev,
    # No genera el instalador (dist\FileSync-Pro-Setup.exe), solo el .exe.
    # (La firma de codigo no tiene opcion: se hace sola si el .env trae un certificado.)
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

$localEnvPath = Join-Path $PWD ".env"
$desktopConfigPath = Join-Path $PWD "desktop_runtime_config.json"
$envFileValues = @{}

if (Test-Path $localEnvPath) {
    foreach ($rawLine in Get-Content $localEnvPath) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }
        $parts = $line.Split("=", 2)
        $envFileValues[$parts[0].Trim()] = $parts[1].Trim()
    }
}

function Get-ConfigValue {
    param(
        [string]$Key,
        [string]$Default = ""
    )

    $processValue = [Environment]::GetEnvironmentVariable($Key)
    if (-not [string]::IsNullOrWhiteSpace($processValue)) {
        return $processValue.Trim()
    }
    if ($envFileValues.ContainsKey($Key) -and -not [string]::IsNullOrWhiteSpace($envFileValues[$Key])) {
        return $envFileValues[$Key].Trim()
    }
    return $Default
}

function Get-JsonObjectValue {
    param(
        [string]$RawValue
    )

    $trimmedValue = ""
    if ($null -ne $RawValue) {
        $trimmedValue = $RawValue.Trim()
    }
    if ([string]::IsNullOrWhiteSpace($trimmedValue)) {
        return @{}
    }
    try {
        $parsedValue = $trimmedValue | ConvertFrom-Json
        if ($null -eq $parsedValue) {
            return @{}
        }
        return $parsedValue
    } catch {
        Write-Warning "No se pudo interpretar como JSON: $trimmedValue"
        return @{}
    }
}

if (Test-Path $localEnvPath) {
    $envContent = Get-Content $localEnvPath -Raw
    if ($envContent -match "(?m)^\s*FILESYNC_PRO_DEV_MODE\s*=\s*1\s*$") {
        Write-Warning "FILESYNC_PRO_DEV_MODE=1 esta activo en .env. No distribuyas el ejecutable junto con ese archivo si quieres respetar licencias por plan."
    }
}

$embeddedDesktopConfig = [ordered]@{
    FILESYNC_PRO_LICENSE_REDEEM_URL     = Get-ConfigValue -Key "FILESYNC_PRO_LICENSE_REDEEM_URL"
    FILESYNC_PRO_LICENSE_SIGNING_SECRET = Get-ConfigValue -Key "FILESYNC_PRO_LICENSE_SIGNING_SECRET"
    FILESYNC_PRO_DEFAULT_PLAN_CODE      = Get-ConfigValue -Key "FILESYNC_PRO_DEFAULT_PLAN_CODE" -Default "basica"
    WOMPI_SUBSCRIPTION_LINK_MAP         = Get-JsonObjectValue -RawValue (Get-ConfigValue -Key "WOMPI_SUBSCRIPTION_LINK_MAP" -Default "{}")
}

if ([string]::IsNullOrWhiteSpace($embeddedDesktopConfig.FILESYNC_PRO_LICENSE_REDEEM_URL)) {
    throw "Falta FILESYNC_PRO_LICENSE_REDEEM_URL en tu .env (la URL del webhook 'Canje de codigo' de Make). Sin ella el ejecutable no puede activar licencias."
}
if ([string]::IsNullOrWhiteSpace($embeddedDesktopConfig.FILESYNC_PRO_LICENSE_SIGNING_SECRET)) {
    Write-Warning "FILESYNC_PRO_LICENSE_SIGNING_SECRET está vacío (agrégalo a tu .env). Sin él, el ejecutable no podrá activar ningún código de licencia."
}

$embeddedDesktopConfig | ConvertTo-Json -Depth 6 | Set-Content $desktopConfigPath -Encoding UTF8

# --- Firma de codigo (opcional) -------------------------------------------------------------------
# Se firma SOLO si el .env trae un certificado (igual que en CipherVault). Sus datos nunca se escriben
# en desktop_runtime_config.json, asi que no viajan dentro del .exe.
#   Opcion A (certificado en el almacen de Windows, token USB o Certum SimplySign):
#       FILESYNC_SIGN_CERT_THUMBPRINT=<huella SHA-1>
#   Opcion B (archivo .pfx):  FILESYNC_SIGN_PFX=C:\ruta\cert.pfx  y  FILESYNC_SIGN_PFX_PASSWORD=...
# Si hay certificado pero falta signtool o el certificado no esta disponible, avisa y sigue sin firmar.
function Find-SignTool {
    $configured = Get-ConfigValue -Key "FILESYNC_SIGNTOOL"
    if ($configured -and (Test-Path $configured)) { return $configured }
    $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    foreach ($root in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $root) { continue }
        $kits = Join-Path $root "Windows Kits\10\bin"
        if (-not (Test-Path $kits)) { continue }
        $found = Get-ChildItem -Path $kits -Filter signtool.exe -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.DirectoryName -match "\\x64$" } |
            Sort-Object { $_.Directory.Parent.Name } -Descending |
            Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    return $null
}

$signThumbprint = ((Get-ConfigValue -Key "FILESYNC_SIGN_CERT_THUMBPRINT") -replace "[^0-9A-Fa-f]", "").ToUpperInvariant()
$signPfx = Get-ConfigValue -Key "FILESYNC_SIGN_PFX"
$signPfxPassword = Get-ConfigValue -Key "FILESYNC_SIGN_PFX_PASSWORD"
$signTimestampUrl = Get-ConfigValue -Key "FILESYNC_SIGN_TIMESTAMP_URL" -Default "http://time.certum.pl"
$signToolPath = $null
$signSubject = $null
$Sign = $false

if ($signThumbprint -or $signPfx) {
    # Se comprueba todo ANTES de la compilacion larga.
    $signToolPath = Find-SignTool
    if (-not $signToolPath) {
        Write-Warning "Hay certificado configurado pero no se encontro signtool.exe. Instala el Windows SDK (componente 'Signing Tools'): winget install Microsoft.WindowsSDK.10.0.26100. Se compilara SIN firmar."
    } elseif ($signThumbprint) {
        if ($signThumbprint.Length -ne 40) {
            Write-Warning "FILESYNC_SIGN_CERT_THUMBPRINT no es una huella SHA-1 valida (40 caracteres). Se compilara SIN firmar."
        } else {
            $signCert = @(Get-ChildItem -Path Cert:\CurrentUser\My, Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
                Where-Object { $_.Thumbprint -eq $signThumbprint }) | Select-Object -First 1
            if (-not $signCert) {
                Write-Warning "El certificado $signThumbprint no esta en el almacen de Windows (si es de Certum SimplySign, abre SimplySign Desktop e inicia sesion). Se compilara SIN firmar."
            } elseif ($signCert.NotAfter -lt (Get-Date)) {
                Write-Warning "El certificado de firma vencio el $($signCert.NotAfter.ToString('yyyy-MM-dd')). Se compilara SIN firmar."
            } else {
                $Sign = $true
                $signSubject = "$($signCert.Subject) (vence $($signCert.NotAfter.ToString('yyyy-MM-dd')))"
            }
        }
    } elseif (-not (Test-Path $signPfx)) {
        throw "No existe el certificado indicado en FILESYNC_SIGN_PFX: $signPfx"
    } else {
        $Sign = $true
        $signSubject = "archivo $signPfx"
    }
    if ($Sign) { Write-Host "Firma de codigo: $signSubject" }
}

# Argumentos de signtool para el certificado configurado (huella o .pfx).
function Get-SignCertArgs {
    if ($signThumbprint) { return @("/sha1", $signThumbprint) }
    $certArgs = @("/f", $signPfx)
    if ($signPfxPassword) { $certArgs += @("/p", $signPfxPassword) }
    return $certArgs
}

function Invoke-CodeSign {
    param([string[]]$Paths, [string]$Description = "FileSync Pro")

    if (-not $Sign -or -not $Paths -or $Paths.Count -eq 0) { return }
    # /tr + /td: sello de tiempo RFC 3161, para que la firma siga valida cuando el certificado venza.
    $certArgs = Get-SignCertArgs
    & $signToolPath sign @certArgs /fd sha256 /tr $signTimestampUrl /td sha256 /d $Description @Paths | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "signtool no pudo firmar (revisa el certificado y la conexion al servidor de sello de tiempo): $($Paths -join ', ')" }
    & $signToolPath verify /pa /q @Paths | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "La firma no se pudo verificar: $($Paths -join ', ')" }
    foreach ($signedPath in $Paths) { Write-Host "Firmado:" $signedPath -ForegroundColor Green }
}

function Get-PythonCommand {
    param(
        [string]$RequestedVersion,
        [string]$ProjectVenvPath
    )

    $venvPython = Join-Path $PWD "$ProjectVenvPath\Scripts\python.exe"
    if (Test-Path $venvPython) {
        return @($venvPython)
    }

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @("py", "-$RequestedVersion")
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        return @($pythonCmd.Source)
    }

    throw "No se encontro Python. Instala Python $RequestedVersion y vuelve a ejecutar este script."
}

$pythonCmd = Get-PythonCommand -RequestedVersion $PythonVersion -ProjectVenvPath $VenvPath
$venvPython = Join-Path $PWD "$VenvPath\Scripts\python.exe"
$distExePath = Join-Path $PWD "dist\FileSync Pro.exe"
$devExeDir = Join-Path $PWD "dist_dev"
$devExePath = Join-Path $devExeDir "FileSync Pro DEV.exe"
$buildFolderPath = Join-Path $PWD "build\FileSync Pro"

if (-not (Test-Path $venvPython)) {
    if ($pythonCmd.Count -eq 2) {
        & $pythonCmd[0] $pythonCmd[1] -m venv $VenvPath
    } else {
        & $pythonCmd[0] -m venv $VenvPath
    }
}

& $venvPython -m pip install --upgrade pip
& $venvPython -m pip install -r requirements-desktop.txt

# --- OCR: preparar Tesseract para incluirlo dentro del .exe -------------------------------------
# `FileSync Pro.spec` empaqueta vendor\tesseract (si existe) y la app lo usa en vez del Tesseract del
# sistema, asi el OCR funciona en un PC que no lo tenga instalado. vendor\ no se versiona.
$vendorTesseractPath = Join-Path $PWD "vendor\tesseract"

function Find-TesseractInstall {
    param([string]$Requested)

    $candidates = New-Object System.Collections.Generic.List[string]
    if ($Requested) { $candidates.Add($Requested) }
    if ($env:TESSERACT_DIR) { $candidates.Add($env:TESSERACT_DIR) }
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if ($root) { $candidates.Add((Join-Path $root "Tesseract-OCR")) }
    }
    if ($env:LOCALAPPDATA) { $candidates.Add((Join-Path $env:LOCALAPPDATA "Programs\Tesseract-OCR")) }
    $onPath = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($onPath) { $candidates.Add((Split-Path $onPath.Source -Parent)) }

    foreach ($candidate in $candidates) {
        # Una instalacion completa tiene tesseract.exe, sus DLL y la carpeta tessdata (un "shim" de
        # Chocolatey, por ejemplo, solo tiene el .exe y no sirve para copiar).
        if ((Test-Path (Join-Path $candidate "tesseract.exe")) -and
            (Test-Path (Join-Path $candidate "tessdata")) -and
            (Get-ChildItem -Path $candidate -Filter "*.dll" -File -ErrorAction SilentlyContinue | Select-Object -First 1)) {
            return $candidate
        }
    }
    return $null
}

function Save-TessdataFile {
    param([string]$Language, [string]$Destination)

    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $url = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/$Language.traineddata"
    Write-Host "Descargando idioma '$Language' de $url"
    Invoke-WebRequest -Uri $url -OutFile $Destination -UseBasicParsing
}

function Initialize-VendorTesseract {
    param([string]$Requested, [string]$TargetDir, [string]$PythonExe, [bool]$Slim)

    $sourceDir = Find-TesseractInstall -Requested $Requested
    if (-not $sourceDir) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            Write-Host "Tesseract OCR no esta instalado: instalandolo con winget (puede pedir permisos de administrador)..."
            & winget install --id UB-Mannheim.TesseractOCR --exact --silent --accept-package-agreements --accept-source-agreements
            $sourceDir = Find-TesseractInstall -Requested $Requested
        }
    }
    if (-not $sourceDir) {
        throw "No se encontro Tesseract OCR. Instalalo (https://github.com/UB-Mannheim/tesseract/wiki), o indica su carpeta con -TesseractDir, o usa -SkipOcr para compilar sin OCR."
    }
    Write-Host "Tesseract encontrado en: $sourceDir"

    if (Test-Path $TargetDir) {
        Remove-Item $TargetDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $TargetDir | Out-Null

    # Solo el motor (tesseract.exe + DLL): se dejan fuera los ~20 programas de entrenamiento y el desinstalador.
    Copy-Item -Path (Join-Path $sourceDir "tesseract.exe") -Destination $TargetDir
    Get-ChildItem -Path $sourceDir -Filter "*.dll" -File | Copy-Item -Destination $TargetDir

    # Idiomas: la app lee con lang="spa+eng". Se copian los instalados o se descargan (tessdata_fast).
    $sourceTessdata = Join-Path $sourceDir "tessdata"
    $targetTessdata = Join-Path $TargetDir "tessdata"
    New-Item -ItemType Directory -Path $targetTessdata | Out-Null
    foreach ($subfolder in @("configs", "tessconfigs")) {
        $subfolderPath = Join-Path $sourceTessdata $subfolder
        if (Test-Path $subfolderPath) {
            Copy-Item -Path $subfolderPath -Destination $targetTessdata -Recurse
        }
    }
    foreach ($language in @("eng", "spa")) {
        $sourceFile = Join-Path $sourceTessdata "$language.traineddata"
        $targetFile = Join-Path $targetTessdata "$language.traineddata"
        if (Test-Path $sourceFile) {
            Copy-Item -Path $sourceFile -Destination $targetFile
        } else {
            Save-TessdataFile -Language $language -Destination $targetFile
        }
        if (-not (Test-Path $targetFile) -or (Get-Item $targetFile).Length -lt 500KB) {
            throw "El archivo de idioma '$language' no se copio o descargo bien: $targetFile"
        }
    }

    # Licencia de Tesseract (Apache 2.0): debe acompanar a los binarios que se redistribuyen.
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://raw.githubusercontent.com/tesseract-ocr/tesseract/main/LICENSE" `
            -OutFile (Join-Path $TargetDir "LICENSE-tesseract.txt") -UseBasicParsing
    } catch {
        Write-Warning "No se pudo descargar la licencia de Tesseract. Agregala a mano en $TargetDir (ver THIRD_PARTY_NOTICES.md)."
    }

    # Adelgazar: se borran las DLL que tesseract.exe no necesita (son de las herramientas de entrenamiento) y se
    # quita la informacion de depuracion de las demas (libtesseract-5.dll pesa ~100 MB casi solo por eso).
    # tools\slim_tesseract_win.py lo comprueba todo y termina leyendo una imagen con el resultado. Si algo falla,
    # se restaura la copia completa: el .exe pesa mas, pero funciona.
    if ($Slim) {
        $slimScript = Join-Path $PWD "tools\slim_tesseract_win.py"
        $slimOk = $false
        if (Test-Path $slimScript) {
            $slimPreviousPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = "Continue"
                & $PythonExe $slimScript $TargetDir --selftest
                $slimOk = ($LASTEXITCODE -eq 0)
            } finally {
                $ErrorActionPreference = $slimPreviousPreference
            }
        } else {
            Write-Warning "No existe $slimScript."
        }
        if (-not $slimOk) {
            Write-Warning "No se pudo adelgazar Tesseract: se usa la copia completa (el .exe pesara ~100 MB mas)."
            Copy-Item -Path (Join-Path $sourceDir "tesseract.exe") -Destination $TargetDir -Force
            Get-ChildItem -Path $sourceDir -Filter "*.dll" -File | Copy-Item -Destination $TargetDir -Force
        }
    } else {
        Write-Host "-NoSlim: se conserva Tesseract completo."
    }

    # Prueba: la copia debe arrancar SOLA (sin depender de la instalacion original) y ver spa y eng.
    $previousPrefix = $env:TESSDATA_PREFIX
    $previousPreference = $ErrorActionPreference
    try {
        $env:TESSDATA_PREFIX = $targetTessdata
        $ErrorActionPreference = "Continue"
        $languages = (& (Join-Path $TargetDir "tesseract.exe") --list-langs 2>&1 | Out-String)
    } finally {
        $ErrorActionPreference = $previousPreference
        $env:TESSDATA_PREFIX = $previousPrefix
    }
    if (($languages -notmatch "(?m)^spa\s*$") -or ($languages -notmatch "(?m)^eng\s*$")) {
        throw "La copia de Tesseract en $TargetDir no arranca o no ve los idiomas spa y eng. Salida:`n$languages"
    }
    $sizeMb = [math]::Round((Get-ChildItem -Path $TargetDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    Write-Host "OCR listo en $TargetDir ($sizeMb MB): motor + idiomas spa y eng."
}

if ($SkipOcr) {
    Write-Warning "-SkipOcr: el .exe se genera SIN OCR. En un PC sin Tesseract instalado, leer imagenes de facturas fallara."
    if (Test-Path $vendorTesseractPath) {
        Remove-Item $vendorTesseractPath -Recurse -Force
    }
} else {
    Initialize-VendorTesseract -Requested $TesseractDir -TargetDir $vendorTesseractPath -PythonExe $venvPython -Slim (-not $NoSlim)
    if ($Sign) {
        # El adelgazado le quita la firma original a Tesseract (quedaria invalida); se firma con la tuya.
        $tesseractBinaries = @(Get-ChildItem -Path $vendorTesseractPath -File |
            Where-Object { $_.Extension -in @(".exe", ".dll") } | ForEach-Object { $_.FullName })
        Invoke-CodeSign -Paths $tesseractBinaries -Description "Tesseract OCR (FileSync Pro)"
    }
}

if (Test-Path $buildFolderPath) {
    Remove-Item $buildFolderPath -Recurse -Force
}
if (Test-Path $distExePath) {
    Remove-Item $distExePath -Force
}

if ($Dev) {
    # Huella de ESTE equipo: el .exe de desarrollador solo da acceso aqui.
    $devHash = (& $venvPython -c "import Organizador as O; print(O.LicenseManager.get_dev_device_hash())" | Select-Object -Last 1).Trim()
    if ($devHash -notmatch '^[0-9a-f]{64}$') {
        throw "No se pudo calcular la huella de este equipo para el .exe de desarrollador."
    }
    $devConfig = [ordered]@{}
    foreach ($entry in $embeddedDesktopConfig.GetEnumerator()) { $devConfig[$entry.Key] = $entry.Value }
    $devConfig["FILESYNC_PRO_DEV_DEVICE_HASHES"] = $devHash
    $devConfig | ConvertTo-Json -Depth 6 | Set-Content $desktopConfigPath -Encoding UTF8
}

try {
    & $venvPython -m PyInstaller --clean --noconfirm "FileSync Pro.spec"
} finally {
    if ($Dev) {
        # Se deja el JSON como para un build normal: la huella solo va dentro del .exe de desarrollador.
        $embeddedDesktopConfig | ConvertTo-Json -Depth 6 | Set-Content $desktopConfigPath -Encoding UTF8
    }
}

if (-not (Test-Path $distExePath)) {
    throw "No se genero el ejecutable esperado en $distExePath"
}

if ($Dev) {
    New-Item -ItemType Directory -Force -Path $devExeDir | Out-Null
    Move-Item -Path $distExePath -Destination $devExePath -Force
    $distExePath = $devExePath
    Write-Warning "Ejecutable de DESARROLLADOR: solo funciona en este PC. No lo compartas ni lo subas a ningun lado."
}

Invoke-CodeSign -Paths @($distExePath)

# --- Instalador (Inno Setup) ---------------------------------------------------------------------
$installerPath = $null
if (-not $Dev -and -not $SkipInstaller) {
    # Inno Setup 6 se instala en "Program Files (x86)" y el 7 en "Program Files". Se buscan las dos
    # (con ProgramW6432, para que funcione aunque PowerShell sea de 32 bits), la carpeta por usuario,
    # el registro de programas instalados y, si hace falta, FILESYNC_ISCC en el .env.
    $isccCandidates = New-Object System.Collections.Generic.List[string]
    $isccConfigured = Get-ConfigValue -Key "FILESYNC_ISCC"
    if ($isccConfigured) { $isccCandidates.Add($isccConfigured) }
    $isccOnPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($isccOnPath) { $isccCandidates.Add($isccOnPath.Source) }
    $uninstallKeys = @(
        "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*",
        "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*"
    )
    Get-ItemProperty -Path $uninstallKeys -ErrorAction SilentlyContinue |
        Where-Object { $_.DisplayName -like "Inno Setup*" -and $_.InstallLocation } |
        Sort-Object DisplayName -Descending |
        ForEach-Object { $isccCandidates.Add((Join-Path $_.InstallLocation "ISCC.exe")) }
    foreach ($root in @($env:ProgramW6432, $env:ProgramFiles, ${env:ProgramFiles(x86)}, (Join-Path $env:LOCALAPPDATA "Programs"))) {
        if (-not $root -or -not (Test-Path $root)) { continue }
        Get-ChildItem -Path $root -Directory -Filter "Inno Setup*" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { $isccCandidates.Add((Join-Path $_.FullName "ISCC.exe")) }
    }
    $isccPath = $isccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

    if (-not $isccPath) {
        Write-Warning "No se encontro Inno Setup (ISCC.exe): se genero el .exe pero NO el instalador. Instalalo con: winget install JRSoftware.InnoSetup  -- o, si ya esta instalado, pon su ruta en el .env: FILESYNC_ISCC=C:\Program Files\Inno Setup 7\ISCC.exe  (o usa -SkipInstaller para no ver este aviso)."
    } else {
        $versionMatch = Select-String -Path (Join-Path $PWD "Organizador.py") -Pattern '^APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
        if (-not $versionMatch) { throw "No se encontro APP_VERSION en Organizador.py." }
        $appVersion = $versionMatch.Matches[0].Groups[1].Value
        $isccArgs = @("/Q", "/DAppVersion=$appVersion")
        if ($Sign) {
            # Inno Setup llama a este comando para firmar el instalador y el desinstalador.
            # $q = comillas y $f = archivo a firmar (los reemplaza Inno Setup, no PowerShell).
            $innoCertArgs = if ($signThumbprint) { '/sha1 ' + $signThumbprint } else {
                '/f $q' + $signPfx + '$q' + $(if ($signPfxPassword) { ' /p $q' + $signPfxPassword + '$q' } else { '' })
            }
            $innoSignCommand = '$q' + $signToolPath + '$q sign ' + $innoCertArgs +
                ' /fd sha256 /tr ' + $signTimestampUrl + ' /td sha256 /d $qFileSync Pro$q $f'
            $isccArgs += @("/DSIGN", "/Sfsps=$innoSignCommand")
        }
        $isccArgs += (Join-Path $PWD "installer\FileSyncPro.iss")
        Write-Host "Inno Setup: $isccPath"
        & $isccPath @isccArgs
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup no pudo generar el instalador." }
        $installerPath = Join-Path $PWD "dist\FileSync-Pro-Setup.exe"
        if (-not (Test-Path $installerPath)) { throw "No se genero el instalador esperado en $installerPath" }
        if ($Sign) {
            & $signToolPath verify /pa /q $installerPath | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "La firma del instalador no se pudo verificar." }
            Write-Host "Firmado:" $installerPath -ForegroundColor Green
        }
    }
}

$exeInfo = Get-Item $distExePath
Write-Host "Build listo:" $exeInfo.FullName
Write-Host "Ultima modificacion:" $exeInfo.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
Write-Host "Tamano (bytes):" $exeInfo.Length
Write-Host "SHA-256:" (Get-FileHash $distExePath -Algorithm SHA256).Hash
if ($installerPath) {
    $installerInfo = Get-Item $installerPath
    Write-Host "Instalador listo:" $installerInfo.FullName "($([math]::Round($installerInfo.Length / 1MB, 1)) MB)"
    Write-Host "SHA-256 instalador:" (Get-FileHash $installerPath -Algorithm SHA256).Hash
}
if ($Sign) {
    Write-Host "Firmado con: $signSubject" -ForegroundColor Green
} elseif (-not $Dev) {
    Write-Host ""
    Write-Host "El .exe NO esta firmado. Para quitar los avisos de SmartScreen y antivirus, configura" -ForegroundColor Yellow
    Write-Host "FILESYNC_SIGN_CERT_THUMBPRINT o FILESYNC_SIGN_PFX en .env (ver README, 'Firma de codigo')." -ForegroundColor Yellow
}
if ($SkipOcr) {
    Write-Host "OCR: NO incluido (-SkipOcr)."
} else {
    Write-Host "OCR: incluido (Tesseract + idiomas spa y eng)."
    Write-Host "Comprobacion: Start-Process -Wait '$($exeInfo.FullName)' -ArgumentList '--ocr-selftest','ocr.txt'; Get-Content ocr.txt   (debe decir resultado=OK)"
}