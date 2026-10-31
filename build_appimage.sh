#!/bin/bash
# Build MeshMonCore-x86_64.AppImage from source.
#
# Creates a self-contained AppDir (bundled python3.12 interpreter +
# dependencies installed from PyPI + the meshmoncore package), then packs it
# with appimagetool (downloaded on demand on first build).
#
# Requirements: Linux, python3.12 with pip, curl (or wget).
# Usage:  ./build_appimage.sh
set -e
cd "$(dirname "$0")"

APPDIR=build/AppDir
SP="$APPDIR/usr/lib/python3.12/site-packages"
PY=$(command -v python3.12)
[ -n "$PY" ] || { echo "error: python3.12 not found"; exit 1; }

echo "== preparing AppDir =="
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/lib"
cp "$PY" "$APPDIR/usr/bin/python3.12"
# the standard library must live inside the AppDir: PYTHONHOME points here,
# and without it the interpreter cannot even import 'encodings'
cp -a /usr/lib/python3.12 "$APPDIR/usr/lib/python3.12"
mkdir -p "$SP"
# libpython so the AppImage runs on systems without the system python
LIBPY=$(ls /usr/lib/x86_64-linux-gnu/libpython3.12.so.1.0 2>/dev/null || true)
[ -n "$LIBPY" ] && cp "$LIBPY" "$APPDIR/usr/lib/"

echo "== installing pinned dependencies from PyPI (this downloads ~100 MB) =="
python3.12 -m pip install --no-compile --target "$SP" -r requirements.txt

echo "== bundling libxcb-cursor (needed by Qt xcb, absent from wheels) =="
# locate a copy: system package -> apt-get download (uses the machine's
# own repositories, no sudo, always the version that exists there)
LIBSRC=""
for c in /usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0.0.0 \
         /usr/lib/x86_64-linux-gnu/libxcb-cursor.so.0; do
    [ -f "$c" ] && LIBSRC="$c" && break
done
if [ -z "$LIBSRC" ]; then
    mkdir -p build
    (cd build && apt-get download libxcb-cursor0) || true
    dpkg-deb -x build/libxcb-cursor0_*.deb build/deb-root 2>/dev/null || true
    LIBSRC=$(find build/deb-root -name "libxcb-cursor.so.0.0.0" 2>/dev/null | head -1)
fi
if [ -z "$LIBSRC" ]; then
    echo "error: libxcb-cursor not found. Install it with:"
    echo "  sudo apt install libxcb-cursor0"
    echo "then re-run this script."
    exit 1
fi
# -L: the deb ships .so.0 as a symlink to .so.0.0.0 — copy real content
cp -aL "$LIBSRC" "$APPDIR/usr/lib/libxcb-cursor.so.0"
cp -aL "$LIBSRC" "$SP/PySide6/Qt/lib/libxcb-cursor.so.0" 
cp -a "$LIBSRC" "$APPDIR/usr/lib/libxcb-cursor.so.0.0.0"
ln -sf libxcb-cursor.so.0.0.0 "$APPDIR/usr/lib/libxcb-cursor.so.0"

echo "== verifying no unresolved libraries (AppRun library path) =="
if LD_LIBRARY_PATH="$APPDIR/usr/lib:$SP/PySide6/Qt/lib" \
   ldd "$SP/PySide6/Qt/plugins/platforms/libqxcb.so" | grep -q "not found"; then
    LD_LIBRARY_PATH="$APPDIR/usr/lib:$SP/PySide6/Qt/lib" \
        ldd "$SP/PySide6/Qt/plugins/platforms/libqxcb.so" | grep "not found"
    echo "error: unresolved libraries above — the AppImage would not start"
    exit 1
fi

echo "== installing meshmoncore =="
cp -a meshmoncore "$SP/meshmoncore"
find "$SP/meshmoncore" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

echo "== launcher + desktop integration =="
cat > "$APPDIR/AppRun" <<'RUN'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
export PYTHONHOME="$HERE/usr"
export PYTHONPATH="$HERE/usr/lib/python3.12/site-packages"
export QT_QPA_PLATFORM_PLUGIN_PATH="$HERE/usr/lib/python3.12/site-packages/PySide6/Qt/plugins"
export LD_LIBRARY_PATH="$HERE/usr/lib:$HERE/usr/lib/python3.12/site-packages/PySide6/Qt/lib:${LD_LIBRARY_PATH}"
exec "$HERE/usr/bin/python3.12" -m meshmoncore "$@"
RUN
chmod +x "$APPDIR/AppRun"
cp meshmoncore/meshmoncore.png "$APPDIR/meshmoncore.png"
ln -sf meshmoncore.png "$APPDIR/.DirIcon"
cat > "$APPDIR/meshmoncore.desktop" <<'DESK'
[Desktop Entry]
Type=Application
Name=MeshMonCore
Comment=MeshCore Repeater Monitor — battery, temperature & traffic history over guest access
Exec=AppRun
Icon=meshmoncore
Categories=Network;Utility;Science;
Terminal=false
StartupWMClass=MeshMonCore
DESK

echo "== packing AppImage =="
if [ ! -f tools_appimagetool.AppImage ]; then
    echo "downloading appimagetool…"
    curl -L --fail -o tools_appimagetool.AppImage \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage \
        || wget -O tools_appimagetool.AppImage \
        https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x tools_appimagetool.AppImage
fi
./tools_appimagetool.AppImage --appimage-extract-and-run --no-appstream \
    "$PWD/$APPDIR" "$PWD/MeshMonCore-x86_64.AppImage"

echo "== done =="
ls -la MeshMonCore-x86_64.AppImage
