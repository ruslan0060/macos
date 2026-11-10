# -*- mode: python ; coding: utf-8 -*-
# Exam Viewer — macOS (.app) üçün PyInstaller spec. build_mac.sh işlədir, birbaşa çağırmağa ehtiyac yoxdur.
from PyInstaller.utils.hooks import collect_all, collect_submodules

APP_VERSION = "1.0.0"

datas = [("viewer.html", ".")]
binaries = []
hiddenimports = collect_submodules("uvicorn") + ["certifi"]
for package in ("pdfplumber", "pdfminer"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ExamViewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                  # Mac-də UPX imzanı pozur
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,           # Mac-in öz arxitekturası (Apple Silicon → arm64, Intel → x86_64)
    codesign_identity=None,     # ad-hoc imza (PyInstaller avtomatik edir)
    entitlements_file=None,
    icon="icon.icns",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ExamViewer",
)
app = BUNDLE(
    coll,
    name="ExamViewer.app",
    icon="icon.icns",
    bundle_identifier="az.examviewer.quizviewer",
    version=APP_VERSION,
    info_plist={
        "CFBundleName": "ExamViewer",
        "CFBundleDisplayName": "Quiz Viewer",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
        "NSRequiresAquaSystemAppearance": False,      # tünd rejim
        "LSApplicationCategoryType": "public.app-category.education",
        # proqramın öz daxili serveri http://127.0.0.1 üzərində işləyir
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
