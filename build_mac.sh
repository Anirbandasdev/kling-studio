#!/bin/sh
# Builds "dist/Kling Studio.app": double-clickable, no Python needed to run it.
# Run ON A MAC (PyInstaller can't cross-compile):  sh build_mac.sh
# No Mac? Push this folder to GitHub instead -- .github/workflows/build.yml
# builds the .app (and the Windows .exe) in the cloud via Actions.
# Only this build step needs PyInstaller:  python3 -m pip install pyinstaller
set -eu
cd "$(dirname "$0")"

if ! python3 -m PyInstaller --version >/dev/null 2>&1; then
  echo "PyInstaller is not installed. Install it with:"
  echo "  python3 -m pip install pyinstaller"
  exit 1
fi

# tkinter ships with python.org Python; Homebrew Python needs: brew install python-tk
if ! python3 -c "import tkinter" 2>/dev/null; then
  echo "warning: python3 has no tkinter, so the Settings folder picker will fall back"
  echo "to typing the path. For the picker, use python.org Python or: brew install python-tk"
fi

ICON_ARGS=""
if [ -f "ui/app.icns" ]; then
  ICON_ARGS="--icon ui/app.icns"
elif [ -f "ui/app.ico" ]; then
  echo "note: using no icon (ui/app.ico is Windows-only). Run tools/make_mac_icon.py"
  echo "on this Mac once to create ui/app.icns, then re-run this script."
fi

# NOTE: --add-data uses ':' on Mac (vs ';' on Windows).
# --onedir + --windowed produces dist/Kling Studio.app (recommended: faster
# launch, more reliable than --onefile on macOS).
python3 -m PyInstaller --noconfirm --clean --windowed --onedir \
  --name "Kling Studio" $ICON_ARGS --add-data "ui:ui" "Kling Studio.pyw"

echo ""
echo "Built: $(pwd)/dist/Kling Studio.app"
echo "Send it as a zip:  ditto -c -k --keepParent \"dist/Kling Studio.app\" \"dist/Kling Studio-mac.zip\""
echo "First launch: right-click the app -> Open (unsigned app gatekeeper bypass)."
