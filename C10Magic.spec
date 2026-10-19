# -*- mode: python ; coding: utf-8 -*-
#
# Receita do PyInstaller para o C10 Magic.
#   Gerar:  build_exe.bat   (ou: pyinstaller C10Magic.spec --noconfirm)
#   Saída:  dist\C10Magic.exe  (um arquivo só)

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

hiddenimports = (
    # O Django carrega apps, migrações, comandos e template tags por nome
    collect_submodules("cards")
    + collect_submodules("c10magic")
    + collect_submodules("django.contrib.humanize")
    + collect_submodules("django.contrib.staticfiles")
    + ["truststore", "bs4", "html.parser", "sqlite3"]
)

datas = (
    # Templates e arquivos estáticos do app (o código vai dentro do executável)
    [("cards/templates", "cards/templates"), ("cards/static", "cards/static")]
    + collect_data_files("django.contrib.admin")
    + collect_data_files("django.contrib.humanize")
    + collect_data_files("certifi")
)

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="C10Magic",
    debug=False,
    strip=False,
    upx=False,
    console=True,   # janela simples: fechar a janela encerra o programa
    runtime_tmpdir=None,
)
