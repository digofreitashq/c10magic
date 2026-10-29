# Gera dist\C10Magic-portatil\ e dist\C10Magic-portatil.zip
#
# Pacote sem PyInstaller: o Python oficial "embutido" (python.exe assinado pela
# Python Software Foundation) + as bibliotecas + o projeto como arquivos comuns.
# Antivírus costumam desconfiar de um .exe novo e sem assinatura (alerta IDP,
# "sandbox automático" do Avast), mas não do python.exe oficial.
#
# Uso:  .\build_portable.bat

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$PythonVersion = "3.12.9"   # precisa ser a mesma do venv (os módulos compilados são cp312)
$Cache = "build\cache"
$Out   = "dist\C10Magic-portatil"
$Venv  = "venv\Scripts\python.exe"

if (-not (Test-Path $Venv)) {
    throw "Crie o ambiente primeiro: python -m venv venv; venv\Scripts\python.exe -m pip install -r requirements.txt"
}

$venvVersion = (& $Venv -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
if ($venvVersion -ne $PythonVersion) {
    Write-Warning "O venv usa Python $venvVersion e o pacote usa $PythonVersion; ajuste `$PythonVersion no script."
}

# ---- 1. Python embutido (baixado uma vez)
New-Item -ItemType Directory -Force $Cache | Out-Null
$zip = "$Cache\python-$PythonVersion-embed-amd64.zip"
if (-not (Test-Path $zip)) {
    Write-Host "Baixando o Python $PythonVersion portatil..."
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip" -OutFile $zip
}

if (Test-Path $Out) { Remove-Item $Out -Recurse -Force }
New-Item -ItemType Directory -Force "$Out\python", "$Out\app", "$Out\dados" | Out-Null
Expand-Archive $zip -DestinationPath "$Out\python"

# ---- 2. Caminhos do Python embutido: biblioteca, projeto e site-packages
$tag = ($PythonVersion -split "\.")[0..1] -join ""
Set-Content "$Out\python\python$tag._pth" -Encoding ascii -Value @(
    "python$tag.zip",
    ".",
    "..\app",
    "Lib\site-packages",
    "import site"
)

# ---- 3. Dependencias (as mesmas versoes do requirements.txt, so as necessarias)
Write-Host "Instalando as bibliotecas..."
& $Venv -m pip install --quiet --disable-pip-version-check --no-warn-script-location `
    --target "$Out\python\Lib\site-packages" -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip falhou" }

# ---- 4. Projeto (sem testes nem caches)
foreach ($item in "c10magic", "cards", "manage.py", "launcher.py") {
    Copy-Item $item "$Out\app" -Recurse -Force
}
Get-ChildItem "$Out\app" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Remove-Item "$Out\app\cards\tests.py" -ErrorAction SilentlyContinue
Get-ChildItem "$Out\python\Lib\site-packages" -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Copy-Item icon.ico "$Out\icon.ico"

# ---- 5. Atalhos de abertura
Set-Content "$Out\C10 Magic.bat" -Encoding ascii -Value @(
    "@echo off",
    "title C10 Magic",
    "rem Abre o C10 Magic. Feche esta janela para encerrar.",
    "set C10_DATA_DIR=%~dp0dados",
    "set PYTHONDONTWRITEBYTECODE=1",
    "cd /d `"%~dp0app`"",
    "`"%~dp0python\python.exe`" launcher.py",
    "if errorlevel 1 pause"
)

Set-Content "$Out\Criar atalho na area de trabalho.bat" -Encoding ascii -Value @(
    "@echo off",
    "rem Cria um atalho 'C10 Magic' (com o icone) na area de trabalho.",
    "powershell -NoProfile -ExecutionPolicy Bypass -Command `"`$s=(New-Object -ComObject WScript.Shell).CreateShortcut([Environment]::GetFolderPath('Desktop')+'\C10 Magic.lnk'); `$s.TargetPath='%~dp0C10 Magic.bat'; `$s.WorkingDirectory='%~dp0'; `$s.IconLocation='%~dp0icon.ico'; `$s.Save()`"",
    "echo Atalho criado na area de trabalho.",
    "pause"
)

Set-Content "$Out\LEIA-ME.txt" -Encoding utf8 -Value @(
    "C10 Magic - versao portatil (nao precisa instalar o Python)",
    "",
    "1. Extraia esta pasta inteira em qualquer lugar (nao rode de dentro do zip).",
    "2. Dê dois cliques em 'C10 Magic.bat'. O navegador abre sozinho.",
    "   Feche a janela preta para encerrar o programa.",
    "3. Opcional: 'Criar atalho na area de trabalho.bat' cria um atalho com icone.",
    "",
    "Seus dados (banco de dados) ficam na pasta 'dados'. Para levar a colecao e os",
    "decks de outro computador, copie o arquivo c10magic.sqlite3 para dentro dela",
    "(com o programa fechado)."
)

# ---- 6. Zip padrao (tar do Windows grava caminhos corretos)
$zipOut = "dist\C10Magic-portatil.zip"
if (Test-Path $zipOut) { Remove-Item $zipOut }
& "$env:SystemRoot\System32\tar.exe" -a -c -f $zipOut -C dist C10Magic-portatil
if ($LASTEXITCODE -ne 0) { throw "tar falhou" }

$size = [math]::Round((Get-Item $zipOut).Length / 1MB, 1)
Write-Host ""
Write-Host "Pronto:"
Write-Host "  $Out\C10 Magic.bat"
Write-Host "  $zipOut ($size MB)"
