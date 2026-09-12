"""Builds ui/app.icns from the logo.

Run from the v2 folder:  python3 tools/make_mac_icon.py
Requires ui/app.ico to exist (run tools/make_icon.py first, on any PC).

Two paths: Pillow (works anywhere, used by CI) or sips+iconutil (Mac built-ins).
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
ICO = HERE / "ui" / "app.ico"
ICNS = HERE / "ui" / "app.icns"
# iconset entries: (pixel size, filename, sips size)
ENTRIES = [(16, "icon_16x16.png", 16), (32, "icon_16x16@2x.png", 32),
           (32, "icon_32x32.png", 32), (64, "icon_32x32@2x.png", 64),
           (128, "icon_128x128.png", 128), (256, "icon_128x128@2x.png", 256),
           (256, "icon_256x256.png", 256), (512, "icon_256x256@2x.png", 512),
           (512, "icon_512x512.png", 512), (1024, "icon_512x512@2x.png", 1024)]


def main():
    if not ICO.exists():
        print("ui/app.ico not found. Run tools/make_icon.py first.")
        raise SystemExit(1)

    try:
        from PIL import Image  # type: ignore
    except ImportError:
        Image = None
    if Image is not None:
        # pure-Python path: works on any OS (Windows, Linux, CI), no Mac needed
        img = Image.open(ICO).convert("RGBA")
        img.save(ICNS, sizes=[(16, 16), (32, 32), (128, 128), (256, 256), (512, 512)])
        print(f"wrote {ICNS} ({ICNS.stat().st_size // 1024} KB, via Pillow)")
        return

    if sys.platform != "darwin":  # noqa: SIM108
        print("Install Pillow (python -m pip install pillow) and re-run,")
        print("or run this on a Mac (it uses sips and iconutil).")
        raise SystemExit(1)

    with tempfile.TemporaryDirectory(prefix="pk_icon_") as tmp:
        # .ico -> 1024px png (Pillow if present, else sips can read .ico directly)
        src = Path(tmp) / "base.png"
        try:
            from PIL import Image  # type: ignore
            Image.open(ICO).convert("RGBA").resize((1024, 1024)).save(src)
        except ImportError:
            subprocess.run(["sips", "-s", "format", "png", "-z", "1024", "1024",
                            str(ICO), "--out", str(src)], check=True,
                           stdout=subprocess.DEVNULL)
        iconset = Path(tmp) / "app.iconset"
        iconset.mkdir()
        for _size, name, px in ENTRIES:
            subprocess.run(["sips", "-s", "format", "png", "-z", str(px), str(px),
                            str(src), "--out", str(iconset / name)],
                           check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ICNS)], check=True)
    print(f"wrote {ICNS} ({ICNS.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
