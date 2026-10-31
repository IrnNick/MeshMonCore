"""MeshMonCore UI — dark navy dashboard theme.

Palette, stylesheet and app icon. The design language: near-black navy
background, elevated rounded cards, one blue accent, semantic green/amber/red
for status, muted blue-grey secondary text.
"""
import os

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import (QColor, QIcon, QPainter, QPixmap, QBrush, QPen,
                           QPalette)
from PySide6.QtWidgets import QApplication


# ---- palette ----------------------------------------------------------------
BG        = "#0b111b"   # window background
BG_SIDE   = "#080d15"   # sidebar (slightly darker)
CARD      = "#111a28"   # card / elevated surface
CARD_2    = "#0e1622"   # inset surfaces (chart bg, table alt rows)
BORDER    = "#1e2a3d"   # card borders, dividers
TEXT      = "#e6edf6"   # primary text
TEXT_SUB  = "#8a97a8"   # secondary / captions
TEXT_MUTE = "#5b6878"   # dimmed / disabled
ACCENT    = "#4c8dff"   # primary blue
ACCENT_D  = "#2f6fe0"   # pressed
OK        = "#2fd08c"   # healthy green
WARN      = "#f5a623"   # amber
DANGER    = "#ff5d5d"   # danger red
INPUT_BG  = "#0d1522"   # line edits / combos
PLOT_BG   = "#0d1522"   # pyqtgraph plot background
PLOT_FG   = "#3a4a61"   # pyqtgraph axis/grid foreground

QSS = f"""
* {{
    font-family: "DejaVu Sans";
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow, #Root {{ background: {BG}; }}
QStatusBar {{ background: {BG}; color: {TEXT_SUB}; border-top: 1px solid {BORDER}; }}
QStatusBar::item {{ border: none; }}

/* ---- sidebar ---- */
#SideBar {{ background: {BG_SIDE}; border-right: 1px solid {BORDER}; }}
#SideBrand {{ color: {TEXT}; }}
QListWidget#SideNav {{
    background: transparent; border: none; outline: none;
    padding: 2px 8px 8px 8px;
}}
/* the nav rows are item WIDGETS (NavPill) that carry their own visuals —
   the item chrome itself must stay fully transparent */
QListWidget#SideNav::item, QListWidget#SideNav::item:hover,
QListWidget#SideNav::item:selected {{ background: transparent; }}
QFrame#NavPill {{
    background: transparent; border: none; border-radius: 12px;
    color: {TEXT_SUB};
}}
QFrame#NavPill:hover {{ background: {CARD}; }}
QFrame#NavPill[sel="true"] {{ background: {ACCENT}; color: #ffffff; }}
QLabel#NavChevron {{
    color: rgba(255, 255, 255, 220); font-size: 17px; font-weight: bold;
}}
#BrandRule {{ background: {BORDER}; border: none; max-height: 1px; }}
/* section headers in the sidebar (Connection / Alert rules / Polling):
   accent-colored, letter-spaced and underlined so each function group
   reads as its own block */
QLabel#SideSection {{
    color: {ACCENT}; font-weight: bold; letter-spacing: 2px;
    padding: 8px 0 4px 0; border-bottom: 1px solid {BORDER};
}}

/* ---- cards ---- */
QFrame#Card {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
}}
QFrame#Tile {{
    background: {CARD}; border: 1px solid {BORDER}; border-radius: 12px;
}}
QLabel#CardTitle {{ color: {TEXT}; font-weight: bold; font-size: 11pt; }}
QLabel#CardIcon  {{ color: {ACCENT}; font-size: 12pt; }}
QLabel#Caption   {{ color: {TEXT_SUB}; font-size: 9pt; }}
QLabel#BigValue  {{ color: {TEXT}; font-size: 17pt; font-weight: bold; }}
QLabel#Subtle    {{ color: {TEXT_SUB}; }}

/* ---- inputs ---- */
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit {{
    background: {INPUT_BG}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 5px 6px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus,
QTimeEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {TEXT_MUTE}; background: {CARD_2};
}}
QLineEdit[readOnly="true"] {{ color: {TEXT_SUB}; background: {CARD_2}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
    background: {CARD}; border: 1px solid {BORDER};
    selection-background-color: {ACCENT}; selection-color: white;
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,
QTimeEdit::up-button, QTimeEdit::down-button {{
    background: {CARD_2}; border: none; width: 12px;
}}

/* ---- buttons ---- */
QPushButton {{
    background: {CARD_2}; border: 1px solid {BORDER}; border-radius: 8px;
    padding: 6px 14px; color: {TEXT};
}}
QPushButton:hover {{ border-color: {ACCENT}; color: #ffffff; }}
QPushButton:pressed {{ background: {BG_SIDE}; }}
QPushButton:disabled {{ color: {TEXT_MUTE}; border-color: {BORDER}; }}
QPushButton[accent="true"] {{
    background: {ACCENT}; border: 1px solid {ACCENT}; color: #ffffff;
    font-weight: bold; padding: 6px 20px;
}}
QPushButton[accent="true"]:hover {{ background: {ACCENT_D}; }}
QPushButton[accent="true"]:pressed {{ background: {ACCENT_D}; }}
QPushButton[danger="true"] {{
    background: transparent; border: 1px solid {DANGER}; color: {DANGER};
}}
QPushButton[danger="true"]:hover {{ background: {DANGER}; color: #10161f; }}
QPushButton[danger="true"]:disabled {{ color: {TEXT_MUTE}; border-color: {BORDER}; }}
QPushButton[ghost="true"] {{ background: transparent; border: 1px solid {BORDER}; }}

/* ---- switch (checkable pills) ---- */
QCheckBox#switch {{ color: {TEXT_SUB}; spacing: 8px; }}
QCheckBox#switch::indicator {{
    width: 40px; height: 20px; border-radius: 10px;
    background: {CARD_2}; border: 1px solid {BORDER};
}}
QCheckBox#switch::indicator:checked {{
    background: {ACCENT}; border-color: {ACCENT};
}}

/* ---- table ---- */
QTableWidget {{
    background: {CARD_2}; border: 1px solid {BORDER}; border-radius: 8px;
    gridline-color: {BORDER}; alternate-background-color: {CARD};
    selection-background-color: #1c3557; selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {CARD_2}; color: {TEXT_SUB}; border: none;
    border-bottom: 1px solid {BORDER}; padding: 6px 6px; font-weight: bold;
}}
QTableCornerButton::section {{ background: {CARD_2}; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTE}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}

/* ---- tabs ---- */
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_SUB}; padding: 8px 18px;
    border: none; border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover {{ color: {TEXT}; }}

/* ---- misc ---- */
QProgressBar {{ background: {CARD_2}; border: none; border-radius: 4px; height: 6px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 4px; }}
QProgressBar#tileBar::chunk {{ background: {OK}; }}
QProgressBar[warnChunk="true"]::chunk {{ background: {WARN}; }}
/* pill switch for the alerts toggle */
QPushButton#alertSwitch {{
    border-radius: 12px; padding: 5px 16px; font-weight: bold;
}}
QPushButton#alertSwitch:checked {{
    background: {OK}; border: 1px solid {OK}; color: #08130c;
}}
QPushButton#alertSwitch:!checked {{
    background: {CARD_2}; border: 1px solid {BORDER}; color: {TEXT_SUB};
}}
QLabel#PlotHint {{
    color: {WARN}; background: {CARD_2}; border: 1px solid {BORDER};
    border-radius: 8px; padding: 8px;
}}
QProgressBar#tileBarBat::chunk {{ background: {OK}; border-radius: 4px; }}
QProgressBar#tileBarTemp::chunk {{ background: {WARN}; border-radius: 4px; }}

QToolTip {{
    background: {CARD}; color: {TEXT}; border: 1px solid {BORDER};
    padding: 5px; border-radius: 6px;
}}
QListWidget {{ background: {CARD_2}; border: 1px solid {BORDER}; border-radius: 8px; }}
QListWidget::item {{ padding: 4px 6px; }}
QListWidget::item:selected {{ background: #1c3557; color: {TEXT}; }}
"""


def apply(app: QApplication) -> None:
    app.setStyle("Fusion")
    pal = app.palette()
    pal.setColor(QPalette.Window, QColor(BG))
    pal.setColor(QPalette.WindowText, QColor(TEXT))
    pal.setColor(QPalette.Base, QColor(CARD_2))
    pal.setColor(QPalette.AlternateBase, QColor(CARD))
    pal.setColor(QPalette.Text, QColor(TEXT))
    pal.setColor(QPalette.Button, QColor(CARD_2))
    pal.setColor(QPalette.ButtonText, QColor(TEXT))
    pal.setColor(QPalette.Highlight, QColor(ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ToolTipBase, QColor(CARD))
    pal.setColor(QPalette.ToolTipText, QColor(TEXT))
    pal.setColor(QPalette.Disabled, QPalette.WindowText, QColor(TEXT_MUTE))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT_MUTE))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(TEXT_MUTE))
    app.setPalette(pal)
    f = app.font()
    f.setFamily("DejaVu Sans")
    f.setPointSize(10)
    app.setFont(f)
    app.setStyleSheet(QSS)


def _draw_mark(p, size=256):
    """The MeshMonCore mark on a `size`x`size` canvas: navy rounded square,
    three broadcast arcs, mast and green beacon dot. All geometry scales
    from the 256-px design so every consumer (window icon, make_icon.py's
    raster set) draws the identical mark."""
    k = size / 256.0
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(CARD)))
    p.setPen(QPen(QColor(BORDER), max(1, round(4 * k))))
    p.drawRoundedRect(8 * k, 8 * k, 240 * k, 240 * k, 56 * k, 56 * k)
    # three broadcast arcs + mast, centered on the beacon dot. The fan is
    # offset down so the outer stroke clears the rounded square's border:
    # 139 - 120 - 7 = 12 (the border stroke ends at y = 10)
    p.setPen(QPen(QColor(ACCENT), 14 * k, Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for r in (52, 86, 120):
        p.drawArc((128 - r) * k, (139 - r) * k, 2 * r * k, 2 * r * k,
                  25 * 16, 130 * 16)
    p.drawLine(QPointF(128 * k, 208 * k), QPointF(128 * k, 139 * k))
    p.setBrush(QBrush(QColor(OK)))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawEllipse(QPointF(128 * k, 139 * k), 13 * k, 13 * k)


def app_icon() -> QIcon:
    """Program icon: signal-arc mark on the navy rounded square. Uses the
    shipped 256-px meshmoncore.png (generated by make_icon.py — the same file
    the AppDir ships); redraws programmatically only if the file is
    missing. The file is NEVER rewritten at runtime: saving a small raster
    over the shipped icon used to degrade it to 64 px."""
    here = os.path.dirname(os.path.abspath(__file__))
    ico_path = os.path.join(here, "meshmoncore.png")
    if os.path.exists(ico_path):
        return QIcon(ico_path)
    pm = QPixmap(256, 256)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    _draw_mark(p, 256)
    p.end()
    return QIcon(pm)
