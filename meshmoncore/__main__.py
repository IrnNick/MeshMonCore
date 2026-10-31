"""MeshMonCore entry point."""
import os
import shutil
import sys


def _data_dir():
    """Data dir: MESHMONCORE_DATA (or legacy MESHMON_DATA) env var, default
    ~/.local/share/meshmoncore. The first run after the rename migrates the
    database files from the legacy meshmon dir; the map-tile cache is left
    behind and simply re-downloads."""
    env = os.environ.get("MESHMONCORE_DATA") or os.environ.get("MESHMON_DATA")
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    base = os.path.expanduser("~/.local/share/meshmoncore")
    old = os.path.expanduser("~/.local/share/meshmon")
    if not os.path.exists(base) and os.path.isdir(old):
        os.makedirs(base, exist_ok=True)
        for name in os.listdir(old):
            src = os.path.join(old, name)
            if os.path.isfile(src) and not name.startswith("maptiles"):
                shutil.copy2(src, os.path.join(base, name))
    os.makedirs(base, exist_ok=True)
    return base


def main():
    from PySide6.QtWidgets import QApplication
    from meshmoncore import theme
    from meshmoncore.ui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("MeshMonCore")
    app.setOrganizationName("MeshMonCore")
    app.setDesktopFileName("meshmoncore")   # Wayland app_id -> desktop file
    theme.apply(app)                       # Fusion + dark palette + QSS
    app.setWindowIcon(theme.app_icon())

    base = _data_dir()
    win = MainWindow(db_path=os.path.join(base, "history.sqlite3"))
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
