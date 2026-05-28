param(
    [string]$PythonVersion = "3.13",
    [string]$VenvPath = ".venv313"
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
    FILESYNC_PRO_API_URL = Get-ConfigValue -Key "FILESYNC_PRO_API_URL"
    FILESYNC_PRO_DEFAULT_PLAN_CODE = Get-ConfigValue -Key "FILESYNC_PRO_DEFAULT_PLAN_CODE" -Default "basica"
    MP_SUBSCRIPTION_LINK_MAP = Get-JsonObjectValue -RawValue (Get-ConfigValue -Key "MP_SUBSCRIPTION_LINK_MAP" -Default "{}")
    FILESYNC_PRO_OWNER_HOSTNAMES = Get-ConfigValue -Key "FILESYNC_PRO_OWNER_HOSTNAMES"
    FILESYNC_PRO_OWNER_DEVICE_IDS = Get-ConfigValue -Key "FILESYNC_PRO_OWNER_DEVICE_IDS"
}

if ([string]::IsNullOrWhiteSpace($embeddedDesktopConfig.FILESYNC_PRO_API_URL) -or $embeddedDesktopConfig.FILESYNC_PRO_API_URL -match "tu-dominio") {
    Write-Warning "FILESYNC_PRO_API_URL no apunta a un backend real. El ejecutable podra abrir el pago, pero no podra activar licencias en otros PCs."
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
