param(
    [string]$PythonVersion = "3.13",
    [string]$VenvPath = ".venv313",
    # Carpeta de una instalacion de Tesseract OCR a incluir en el .exe (por defecto se busca sola).
    [string]$TesseractDir = "",
    # Genera el .exe SIN OCR (la lectura de imagenes de facturas exigira Tesseract instalado en cada PC).
    [switch]$SkipOcr,
    # No adelgaza Tesseract: se incluye tal cual esta instalado (el .exe pesa ~100 MB mas).
    [switch]$NoSlim
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
    FILESYNC_PRO_LICENSE_CSV_URL   = Get-ConfigValue -Key "FILESYNC_PRO_LICENSE_CSV_URL" -Default "https://docs.google.com/spreadsheets/d/e/2PACX-1vQvh9IJdUCdyxIfAd3_Dx0PDr1Hm2_a0U5v2KoBG-MBNUO1U3wbZ6dld4rguHxExJlvCOce_uyQVb1X/pub?gid=0&single=true&output=csv"
    FILESYNC_PRO_DEFAULT_PLAN_CODE = Get-ConfigValue -Key "FILESYNC_PRO_DEFAULT_PLAN_CODE" -Default "basica"
    WOMPI_SUBSCRIPTION_LINK_MAP   = Get-JsonObjectValue -RawValue (Get-ConfigValue -Key "WOMPI_SUBSCRIPTION_LINK_MAP" -Default "{}")
}

if ([string]::IsNullOrWhiteSpace($embeddedDesktopConfig.FILESYNC_PRO_LICENSE_CSV_URL)) {
    Write-Warning "FILESYNC_PRO_LICENSE_CSV_URL está vacío. El ejecutable no podrá verificar licencias contra Google Sheets."
}

$embeddedDesktopConfig | ConvertTo-Json -Depth 6 | Set-Content $desktopConfigPath -Encoding UTF8

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
}

if (Test-Path $buildFolderPath) {
    Remove-Item $buildFolderPath -Recurse -Force
}
if (Test-Path $distExePath) {
    Remove-Item $distExePath -Force
}

& $venvPython -m PyInstaller --clean --noconfirm "FileSync Pro.spec"

if (-not (Test-Path $distExePath)) {
    throw "No se genero el ejecutable esperado en $distExePath"
}

$exeInfo = Get-Item $distExePath
Write-Host "Build listo:" $exeInfo.FullName
Write-Host "Ultima modificacion:" $exeInfo.LastWriteTime.ToString("yyyy-MM-dd HH:mm:ss")
Write-Host "Tamano (bytes):" $exeInfo.Length
if ($SkipOcr) {
    Write-Host "OCR: NO incluido (-SkipOcr)."
} else {
    Write-Host "OCR: incluido (Tesseract + idiomas spa y eng)."
    Write-Host "Comprobacion: Start-Process -Wait '$($exeInfo.FullName)' -ArgumentList '--ocr-selftest','ocr.txt'; Get-Content ocr.txt   (debe decir resultado=OK)"
}