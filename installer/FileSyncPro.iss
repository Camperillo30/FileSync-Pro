; Instalador de FileSync Pro para Windows (Inno Setup 6).
; No se compila a mano: lo hace build.ps1 al final, pasándole la versión y, con -Sign, la firma:
;   ISCC.exe /DAppVersion=3.1.0 [/DSIGN /Sfsps=...] installer\FileSyncPro.iss
; Resultado: dist\FileSync-Pro-Setup.exe (nombre fijo, para que el enlace de descarga de GitHub no cambie)
;
; Se instala por usuario (sin pedir permisos de administrador) en
; %LOCALAPPDATA%\Programs\FileSync Pro. Si el usuario lo prefiere, el asistente le deja instalar
; para todos los usuarios en Archivos de programa (eso sí pide administrador).
; Desinstalar NO borra los datos del usuario (%APPDATA%\FileSync Pro: licencia, preferencias,
; cuarentena), así que reinstalar o actualizar conserva la licencia.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "FileSync Pro"
#define AppPublisher "S.C.A"
#define AppExeName "FileSync Pro.exe"
#define AppURL "https://filesyncpro-n8clbgo.gamma.site/"

[Setup]
; AppId identifica la app ante Windows: NO cambiarlo nunca, o las actualizaciones se instalarían
; como un programa aparte.
AppId={{6F3C8A21-4D7B-4E59-9A1E-2B8C5D0F7A13}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}
VersionInfoDescription=Instalador de {#AppName}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
SourceDir=..
OutputDir=dist
OutputBaseFilename=FileSync-Pro-Setup
SetupIconFile=assets\icono.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Si la app está abierta al actualizar, el instalador ofrece cerrarla.
CloseApplications=yes
RestartApplications=no
#ifdef SIGN
; build.ps1 -Sign define "fsps" con signtool y el certificado. Firma el instalador y el desinstalador.
SignTool=fsps
SignedUninstaller=yes
#endif

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
