#!/usr/bin/env bash
# =====================================================================
# Exam Viewer — macOS üçün ExamViewer.app və .dmg yaradır.
# Mac-də Terminal-da bu qovluğa gir və işlət:   bash build_mac.sh
#
# Lazımdır: Python 3.10–3.14 (tövsiyə 3.12/3.13), python.org-dan (https://www.python.org/downloads/macos/).
# Hazır fayllar: dist/ExamViewer.app  və  dist/ExamViewer-mac-<arm64|x86_64>.dmg
# Apple Silicon (M1/M2/M3/M4) Mac-də yığılan versiya Apple Silicon-da,
# Intel Mac-də yığılan versiya Intel-də işləyir.
# =====================================================================
set -euo pipefail
cd "$(dirname "$0")"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Bu skript yalnız macOS-da işləyir. Windows üçün build.bat-dan istifadə et."
  exit 1
fi

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 tapılmadı. https://www.python.org/downloads/macos/ ünvanından qur, sonra yenidən işlət."
  exit 1
fi

"$PY" - <<'PYCHECK'
import sys
v = sys.version_info
if not ((3, 10) <= v[:2] <= (3, 14)):
    sys.exit(f"Python {v.major}.{v.minor} uyğun deyil. Python 3.10–3.14 lazımdır (tövsiyə: 3.12 və ya 3.13, python.org).")
if "/opt/homebrew" in sys.prefix or "/usr/local/Cellar" in sys.prefix:
    print("DİQQƏT: Homebrew Python-u istifadə olunur. Proqram yalnız bu Mac-in macOS versiyasında və daha yenilərində işləyə bilər.")
    print("        Başqa Mac-lərə paylamaq üçün python.org-dan olan Python daha yaxşıdır.")
print(f"Python {v.major}.{v.minor}.{v.micro}  ({sys.executable})")
PYCHECK

for f in launcher.py parse_api.py parse_exam_pdf.py license_guard.py viewer.html icon.icns ExamViewer_mac.spec; do
  [[ -f "$f" ]] || { echo "Fayl tapılmadı: $f"; exit 1; }
done

echo "=== 1/4 Ayrıca Python mühiti və paketlər ==="
rm -rf .venv-mac
"$PY" -m venv .venv-mac
# shellcheck disable=SC1091
source .venv-mac/bin/activate
python -m pip install --upgrade pip
python -m pip install fastapi uvicorn pdfplumber python-multipart pydantic pywebview certifi pyinstaller

echo "=== 2/4 Yoxlama: modullar yüklənir? ==="
python - <<'PYIMPORT'
import certifi, fastapi, pdfplumber, uvicorn, webview  # noqa: F401
import license_guard
print("OK — kompüter:", license_guard.DEVICE_NAME)
PYIMPORT

echo "=== 3/4 ExamViewer.app yaradılır ==="
rm -rf build/ExamViewer_mac dist/ExamViewer dist/ExamViewer.app
python -m PyInstaller --noconfirm --clean ExamViewer_mac.spec

APP="dist/ExamViewer.app"
[[ -d "$APP" ]] || { echo "XƏTA: $APP yaranmadı."; exit 1; }
# yeni yığılmış proqram Mac-in özündə karantinə düşməsin
xattr -cr "$APP" 2>/dev/null || true
codesign --verify --deep "$APP" >/dev/null 2>&1 || codesign --force --deep --sign - "$APP"

echo "=== 4/4 .dmg yaradılır ==="
ARCH="$(uname -m)"
DMG="dist/ExamViewer-mac-${ARCH}.dmg"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
for attempt in 1 2 3; do   # hdiutil bəzən "Resource busy" verir — təkrar yoxlayır
  if hdiutil create -volname "ExamViewer" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null; then break; fi
  [[ $attempt == 3 ]] && { echo "XƏTA: .dmg yaradılmadı (app hazırdır: $APP)"; exit 1; }
  sleep 3
done
rm -rf "$STAGE"

deactivate || true
echo
echo "Hazırdır:"
echo "  $APP"
echo "  $DMG   ($ARCH)"
echo "Yoxlamaq üçün: open \"$APP\""
