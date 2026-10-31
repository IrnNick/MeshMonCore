"""MeshMonCore main window — PySide6 UI (dark Fusion theme)."""
import json
import math
import os
import shutil
import time

import pyqtgraph as pg
from PySide6.QtCore import (QEvent, QObject, QPointF, QRect, QSize, Qt,
                            QTime, QTimer)
from PySide6.QtGui import (QColor, QCursor, QFont, QGuiApplication,
                           QPainter, QPen, QPixmap, QPolygonF)
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QListView,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QRadioButton,
    QScrollArea,
    QSpinBox, QSizePolicy, QStackedWidget,
    QSplitter, QStatusBar, QTableWidget, QTableWidgetItem, QTabWidget,
    QTextEdit, QTimeEdit, QVBoxLayout, QWidget,
)
from PySide6.QtWidgets import QStyle, QStyleOptionComboBox, QStylePainter

from . import theme, __version__
from .mapview import BASEMAPS, SlippyMap
from .radio import RadioWorker
from .store import Store

pg.setConfigOptions(antialias=True, background=theme.PLOT_BG,
                    foreground=theme.PLOT_FG)

SETTINGS_ORG, SETTINGS_APP = "MeshMonCore", "MeshMonCore"


def _settings():
    """QSettings under the MeshMonCore namespace. The first run after
    the rename migrates the legacy meshmon config file (values are
    preserved verbatim)."""
    from PySide6.QtCore import QSettings
    new_path = QSettings(SETTINGS_ORG, SETTINGS_APP).fileName()
    old_path = QSettings("meshmon", "meshmon").fileName()
    if not os.path.exists(new_path) and os.path.exists(old_path):
        os.makedirs(os.path.dirname(new_path), exist_ok=True)
        shutil.copy2(old_path, new_path)
    return QSettings(SETTINGS_ORG, SETTINGS_APP)


METRIC_LABELS = {
    "battery_v":     ("Battery (V)",        "V",   "#4F9DF7"),
    "temperature_c": ("Temperature (°C)",   "°C",  "#F5A623"),
    "noise_floor":   ("Noise floor (dBm)",  "dBm", "#B48EAD"),
    "rssi":          ("RSSI (dBm)",         "dBm", "#F25F5C"),
    "snr":           ("SNR (dB)",           "dB",  "#35C9C9"),
    "airtime_s":     ("TX airtime (s)",     "s",   "#9C6ADE"),
    "rx_airtime_s":  ("RX airtime (s)",     "s",   "#6ADE8F"),
    "uptime_s":      ("Uptime",             "",    "#AAAAAA"),
    "sent_flood":    ("Sent flood",         "",    "#DDDDDD"),
    "sent_direct":   ("Sent direct",        "",    "#DDDDDD"),
    "recv_flood":    ("Recv flood",         "",    "#DDDDDD"),
    "recv_direct":   ("Recv direct",        "",    "#DDDDDD"),
    "recv_errors":   ("RX errors",          "",    "#FF8888"),
    "util_tx":       ("Utilization (TX %)", "%",  "#9c6ade"),
    "util_rx":       ("Utilization (RX %)", "%",  "#35c9c9"),
}
# dropdown order for the chart metric picker
METRIC_ORDER = ["battery_v", "temperature_c", "noise_floor", "rssi", "snr",
                "airtime_s", "rx_airtime_s", "uptime_s", "util_tx",
                "util_rx", "recv_flood", "recv_direct", "sent_flood",
                "sent_direct", "recv_errors"]

# repeater table columns (next UI)
COL_CHK, COL_STAT, COL_NAME, COL_PK, COL_HOPS, COL_VOK, COL_BATT, \
COL_TEMP, COL_RSSI, COL_SNR, COL_NOISE, COL_UTIL, COL_LAST = range(13)

# page order in the sidebar / page stack
PAGE_DASH, PAGE_OVERVIEW, PAGE_MAP, PAGE_LOG, PAGE_SETTINGS = range(5)


class DashItem(QTableWidgetItem):
    """Table item that sorts by an explicit key (numeric metric values,
    timestamps) instead of the displayed text — "10.2 dB" must sort after
    "9.8 dB", which text comparison gets wrong."""

    def __init__(self, text="", key=None):
        super().__init__(text)
        self._key = key if key is not None else text

    def __lt__(self, other):
        try:
            return self._key < other._key
        except (TypeError, AttributeError):
            return self.text() < other.text()

# dashboard: range choices, rank palette, per-metric bar specs and formats
DASH_RANGES = (("Last 24 hours", 24 * 3600), ("Last 7 days", 7 * 86400),
               ("Last 30 days", 30 * 86400))
DASH_COLORS = ["#4c8dff", "#2fd08c", "#f5a623", "#e15fed", "#ff5d5d",
               "#35c9c9", "#c9d35c", "#9c6ade", "#6ade8f", "#ff8ac2"]
# metric -> (bar low, bar high, bar color); metrics not listed get a
# relative-to-max bar
DASH_BARS = {"battery_v": (3.0, 4.2, "#2fd08c"),
             "temperature_c": (-20.0, 50.0, "#f5a623"),
             "rssi": (-120.0, -40.0, "#4c8dff"),
             "snr": (0.0, 20.0, "#9c6ade"),
             "noise_floor": (-130.0, -90.0, "#2fd08c"),
             "util_tx": (0.0, 100.0, "#4c8dff"),
             "util_rx": (0.0, 100.0, "#9c6ade"),
             "uptime_s": (0.0, 14 * 86400.0, "#2fd08c")}
# metrics where "up" is good (green delta) — None means neutral/grey
DASH_UP_GOOD = {"battery_v": True, "snr": True, "rssi": True,
                "noise_floor": True, "uptime_s": True, "recv_errors": False}


def now_age(ts):
    """Seconds since `ts` (epoch)."""
    return time.time() - ts


def _fmt_metric(metric, v):
    """Human formatting for a metric sample value."""
    if v is None:
        return "—"
    if metric == "uptime_s":
        return fmt_duration(int(v))
    if metric in ("rssi", "noise_floor"):
        return f"{v:.0f} dBm"
    if metric == "snr":
        return f"{v:.1f} dB"
    if metric == "battery_v":
        return f"{v:.2f} V"
    if metric == "temperature_c":
        return f"{v:.1f} °C"
    if metric in ("util_tx", "util_rx"):
        return f"{v:.1f} %"
    if metric.startswith(("sent_", "recv_")) or metric == "recv_errors":
        return f"{int(v)}"
    if metric == "airtime_s":
        return f"{v:.0f} s"
    return f"{v:.2f}"


class MultiSelectCombo(QComboBox):
    """QComboBox with checkable items — pick any subset of metrics for the
    chart. The closed state shows the current selection summary."""
    selection_changed = Signal()

    def __init__(self, placeholder="select metrics"):
        super().__init__()
        self._placeholder = placeholder
        self.setView(QListView(self))

    def addCheckItem(self, text, data, checked=False):
        super().addItem(text, data)
        it = self.model().item(self.count() - 1, self.modelColumn())
        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
        it.setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def connect_model(self, handler):
        self.model().itemChanged.connect(lambda _it: handler())

    def checked_data(self):
        out = []
        for i in range(self.count()):
            it = self.model().item(i, self.modelColumn())
            if it is not None and it.checkState() == Qt.Checked:
                out.append(self.itemData(i))
        return out

    def set_checked_data(self, wanted):
        wanted = set(wanted)
        for i in range(self.count()):
            it = self.model().item(i, self.modelColumn())
            it.setCheckState(
                Qt.Checked if self.itemData(i) in wanted else Qt.Unchecked)

    def paintEvent(self, event):
        painter = QStylePainter(self)
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        texts = []
        for i in range(self.count()):
            it = self.model().item(i, self.modelColumn())
            if it is not None and it.checkState() == Qt.Checked:
                texts.append(self.itemText(i))   # friendly labels, not keys
        opt.currentText = ", ".join(texts) if texts else self._placeholder
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, opt)
        painter.drawControl(QStyle.ControlElement.CE_ComboBoxLabel, opt)


class WheelGuard(QObject):
    """Swallows wheel events over the sidebar's value widgets (combo boxes,
    spin boxes, time edits) and scrolls the sections area instead — an
    accidental scroll over a field must not silently change its value."""

    def __init__(self, scroll):
        super().__init__(scroll)
        self._scroll = scroll

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Type.Wheel:
            sb = self._scroll.verticalScrollBar()
            sb.setValue(sb.value() - ev.angleDelta().y())
            return True
        return False


class MiniBar(QProgressBar):
    """Dashboard mini bar: its size hint must be small — the default
    QProgressBar hint (~100+ px) leaked into resizeColumnsToContents and
    inflated every metric column. The bar still fills its cell; the hint
    only matters for the column resize pass."""

    def sizeHint(self):
        return QSize(40, 5)

    def minimumSizeHint(self):
        return QSize(10, 5)


class UptimeDaysAxis(pg.AxisItem):
    """Y-axis for the dashboard's Uptime mini chart. Uptime is plotted in
    DAYS (seconds / 86400) so a multi-year repeater reads '400d' instead of
    the raw-seconds scientific notation (2e+06) the default axis showed."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def tickStrings(self, values, scale, spacing):
        out = []
        for v in values:
            if v >= 10:
                out.append(f"{v:.0f}d")
            elif v >= 1:
                out.append(f"{v:.1f}d")
            elif v > 0:
                out.append(f"{v * 24:.0f}h")
            else:
                out.append("0")
        return out


def fmt_duration(seconds):
    """Seconds -> '2d 4h 03m' / '4h 03m' / '12s'."""
    s = int(seconds)
    if s < 0:
        return "—"
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m:02d}m"
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


class MainWindow(QMainWindow):
    def __init__(self, db_path):
        super().__init__()
        self.setWindowTitle(f"MeshMonCore v{__version__} — MeshCore Repeater Monitor")
        # fit the screen: the old hardcoded 1280x760 put the window's bottom
        # off-screen on small panels (e.g. 1366x768 minus taskbar)
        scr = (QGuiApplication.screenAt(QCursor.pos())
               or QGuiApplication.primaryScreen())
        self._avail = scr.availableGeometry()
        self.resize(min(1500, self._avail.width()),
                    min(1020, self._avail.height()))
        self._db_dir = os.path.dirname(os.path.abspath(db_path))
        self.store = Store(db_path)
        self.radio = RadioWorker(self.store)

        self._selected_pubkey = None
        self._companion_pos = None   # (lat, lon) from the handshake, for links
        self._table_filter = ""
        self._dash_metrics = ["battery_v", "temperature_c", "rssi", "snr",
                              "noise_floor", "uptime_s", "util_tx",
                              "util_rx"]
        self._dash_rank = "last_heard"
        self._dash_range_s = 24 * 3600
        self._dash_n = 10
        self._dash_expand = False   # dashboard table: top-N vs all rows
        self._dash_sig = None
        self._dash_sort = (0, Qt.SortOrder.AscendingOrder)   # (column, order)
        self._plot_curves = {}          # metric -> PlotDataItem
        self._plot_data = {}            # metric -> (xs, ys) for the hover readout
        self._range_s = 7 * 86400      # default range: 7 days

        self._build_ui()
        self._wire_radio()
        self._load_settings()
        # Auto-connect on start: one event-loop tick so the window paints
        # first, then restore the saved companion and connect.
        QTimer.singleShot(0, self._auto_connect)

        # UI refresh timer (table + graph re-poll from sqlite)
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(5000)
        self._ui_timer.timeout.connect(self._refresh_views)
        self._ui_timer.start()
        self._refresh_views()

    # ------------------------------------------------------------------ UI
    def _card(self, glyph, title):
        """Elevated card with an accent glyph + bold title header."""
        card = QFrame()
        card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(8)
        head = QHBoxLayout()
        ic = QLabel(glyph)
        ic.setObjectName("CardIcon")
        t = QLabel(title)
        t.setObjectName("CardTitle")
        head.addWidget(ic)
        head.addWidget(t)
        head.addStretch(1)
        v.addLayout(head)
        return card, v

    def _field(self, caption, widget):
        """Label-above-input mini column (the dashboard's field style)."""
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(3)
        cap = QLabel(caption)
        cap.setObjectName("Caption")
        v.addWidget(cap)
        v.addWidget(widget)
        return w

    def _tile(self, caption, bar_role=None):
        """Stat tile: caption, big value, sub-caption, optional progress bar."""
        f = QFrame()
        f.setObjectName("Tile")
        v = QVBoxLayout(f)
        v.setContentsMargins(12, 10, 12, 10)
        v.setSpacing(4)
        cap = QLabel(caption)
        cap.setObjectName("Caption")
        val = QLabel("—")
        val.setObjectName("BigValue")
        sub = QLabel("")
        sub.setObjectName("Subtle")
        v.addWidget(cap)
        v.addWidget(val)
        v.addWidget(sub)
        bar = None
        if bar_role:
            bar = QProgressBar()
            bar.setObjectName(bar_role)
            bar.setTextVisible(False)
            bar.setRange(0, 100)
            bar.setValue(0)
            v.addWidget(bar)
        f.setMinimumWidth(150)
        return f, val, sub, bar

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("Root")
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- sidebar: brand + nav + connection/alerts/polling ----------------
        side = QFrame()
        side.setObjectName("SideBar")
        side.setFixedWidth(256)
        sv = QVBoxLayout(side)
        sv.setContentsMargins(14, 16, 14, 12)
        sv.setSpacing(6)

        # brand: icon tile + wordmark, hairline rule below
        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        ico = QLabel()
        ico.setPixmap(theme.app_icon().pixmap(44, 44))
        brand_row.addWidget(ico)
        brand = QLabel("MeshMonCore")
        brand.setObjectName("SideBrand")
        brand.setFont(QFont("DejaVu Sans", 17, QFont.Weight.Bold))
        brand_row.addWidget(brand)
        brand_row.addStretch(1)
        sv.addLayout(brand_row)
        rule = QFrame()
        rule.setObjectName("BrandRule")
        rule.setFixedHeight(1)
        sv.addWidget(rule)
        sv.addSpacing(8)

        self.nav = QListWidget()
        self.nav.setObjectName("SideNav")
        self.nav.setIconSize(QSize(22, 22))
        self._nav_rows = []
        for kind, label in (("grid", "Dashboard"), ("home", "Overview"),
                            ("map", "Map"), ("log", "Log"),
                            ("gear", "Settings")):
            row = QWidget()
            pill = QFrame()
            pill.setObjectName("NavPill")
            pl = QHBoxLayout(pill)
            pl.setContentsMargins(12, 9, 14, 9)
            pl.setSpacing(12)
            ic = QLabel()
            lb = QLabel(label)
            lb.setFont(QFont("DejaVu Sans", 10, QFont.Weight.DemiBold))
            chev = QLabel("\u203a")
            chev.setObjectName("NavChevron")
            pl.addWidget(ic)
            pl.addWidget(lb)
            pl.addStretch(1)
            pl.addWidget(chev)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(4, 0, 4, 0)
            rl.addWidget(pill)
            it = QListWidgetItem()
            it.setSizeHint(QSize(0, 46))
            self.nav.addItem(it)
            self.nav.setItemWidget(it, row)
            self._nav_rows.append((pill, ic, chev, kind))
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._on_nav)
        self._on_nav(0)
        # the nav takes exactly its content height — the sections below it
        # scroll, but the menu itself must never clip into a scrollbar
        self.nav.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.nav.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Fixed)
        # QAbstractScrollArea's size hint ignores item widgets — pin the
        # height to the five 46-px rows + the QSS padding (2 top, 8 bottom)
        self.nav.setFixedHeight(self.nav.count() * 46 + 10)
        sv.addWidget(self.nav)

        # the connection / alert rules / polling controls (moved off the
        # Overview page) live here, above the connection status + button; the
        # scroll area keeps small screens usable instead of clipping
        mid_scroll = QScrollArea()
        mid_scroll.setWidgetResizable(True)
        mid_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        mid_scroll.setStyleSheet("QScrollArea { background: transparent; }")
        mid_scroll.viewport().setAutoFillBackground(False)
        # the scrollbar is always shown so the sections' width never changes
        # (a appearing/disappearing 10px scrollbar would cause horizontal
        # jitter/overflow in this narrow column)
        mid_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        mid = QWidget()
        # no stylesheet here on purpose: a selector-less widget stylesheet
        # cascades to EVERY descendant and a widget-level sheet beats the app
        # sheet — "background: transparent;" silently killed the alert
        # switch's green :checked background (plain QWidgets are transparent
        # already, so nothing is lost by dropping it)
        msv = QVBoxLayout(mid)
        msv.setContentsMargins(0, 4, 0, 4)
        msv.setSpacing(8)

        def side_cap(text):
            c = QLabel(text)
            c.setObjectName("SideSection")
            return c

        # -- connection --------------------------------------------------------
        msv.addWidget(side_cap("CONNECTION"))
        self.conn_type = QComboBox()
        self.conn_type.addItems(["Serial (USB)", "TCP (WiFi companion)"])
        msv.addWidget(self._field("Transport", self.conn_type))
        self.conn_serial = QLineEdit()
        self.conn_serial.setPlaceholderText("/dev/ttyACM0")
        self.conn_baud = QSpinBox()
        self.conn_baud.setRange(1200, 1000000)
        self.conn_baud.setValue(115200)
        self.conn_host = QLineEdit()
        self.conn_host.setPlaceholderText("IP of companion, e.g. 192.168.1.42")
        self.conn_port = QSpinBox()
        self.conn_port.setRange(1, 65535)
        self.conn_port.setValue(5000)
        self.conn_serial_host = self._field("Serial port", self.conn_serial)
        self.conn_serial_baud = self._field("Baud rate", self.conn_baud)
        self.conn_tcp_host = self._field("Host", self.conn_host)
        self.conn_tcp_port = self._field("Port", self.conn_port)
        serial_row = QHBoxLayout()
        serial_row.setContentsMargins(0, 0, 0, 0)
        serial_row.setSpacing(6)
        serial_row.addWidget(self.conn_serial_host, 1)
        serial_row.addWidget(self.conn_serial_baud)
        msv.addLayout(serial_row)
        tcp_row = QHBoxLayout()
        tcp_row.setContentsMargins(0, 0, 0, 0)
        tcp_row.setSpacing(6)
        tcp_row.addWidget(self.conn_tcp_host, 1)
        tcp_row.addWidget(self.conn_tcp_port)
        msv.addLayout(tcp_row)
        self.conn_dev_name = QLineEdit()
        self.conn_dev_name.setReadOnly(True)
        self.conn_dev_name.setPlaceholderText("after connect")
        self.conn_dev_name.setToolTip(
            "Node name reported by the companion during the handshake.")
        msv.addWidget(self._field("Device name", self.conn_dev_name))
        self.conn_dev_pk = QLineEdit()
        self.conn_dev_pk.setReadOnly(True)
        self.conn_dev_pk.setPlaceholderText("after connect")
        self.conn_dev_pk.setToolTip(
            "Full 32-byte public key of the companion, hex — select and copy"
            " from here when sharing this device's identity.")
        msv.addWidget(self._field("Public key", self.conn_dev_pk))

        # -- alert rules -------------------------------------------------------
        msv.addWidget(side_cap("ALERT RULES"))
        self.btn_alert_enabled = QPushButton("Alerts off")
        self.btn_alert_enabled.setCheckable(True)
        self.btn_alert_enabled.setChecked(False)
        self.btn_alert_enabled.setObjectName("alertSwitch")
        self.btn_alert_enabled.setToolTip(
            "Turn alerting on or off. When on, after every scheduled alert"
            " check each polled repeater's temperature AND battery voltage"
            " are checked; a channel message is sent when a reading goes"
            " below the low threshold or above the high one. Checks fire"
            " only at the set schedule (interval or time of day), never on"
            " enabling. Starts off on every launch.")
        msv.addWidget(self.btn_alert_enabled)
        temp_cap = QLabel("Temperature (°C)")
        temp_cap.setObjectName("Caption")
        msv.addWidget(temp_cap)
        temp_row = QHBoxLayout()
        temp_row.setContentsMargins(0, 0, 0, 0)
        temp_row.setSpacing(6)
        self.alert_low = QSpinBox()
        self.alert_low.setRange(-60, 100)
        self.alert_low.setValue(-15)
        self.alert_low.setSuffix(" °C")
        self.alert_low.setToolTip("Alert when a reading drops below this.")
        self.alert_high = QSpinBox()
        self.alert_high.setRange(-60, 120)
        self.alert_high.setValue(60)
        self.alert_high.setSuffix(" °C")
        self.alert_high.setToolTip("Alert when a reading rises above this.")
        temp_row.addWidget(self.alert_low, 1)
        temp_row.addWidget(self.alert_high, 1)
        msv.addLayout(temp_row)
        batt_cap = QLabel("Battery voltage (V)")
        batt_cap.setObjectName("Caption")
        msv.addWidget(batt_cap)
        batt_row = QHBoxLayout()
        batt_row.setContentsMargins(0, 0, 0, 0)
        batt_row.setSpacing(6)
        self.alert_low_v = QDoubleSpinBox()
        self.alert_low_v.setRange(0.0, 5.0)
        self.alert_low_v.setDecimals(2)
        self.alert_low_v.setSingleStep(0.05)
        self.alert_low_v.setValue(3.30)
        self.alert_low_v.setToolTip("Alert when a reading drops below this.")
        self.alert_high_v = QDoubleSpinBox()
        self.alert_high_v.setRange(0.0, 5.0)
        self.alert_high_v.setDecimals(2)
        self.alert_high_v.setSingleStep(0.05)
        self.alert_high_v.setValue(4.30)
        self.alert_high_v.setToolTip("Alert when a reading rises above this.")
        batt_row.addWidget(self.alert_low_v, 1)
        batt_row.addWidget(self.alert_high_v, 1)
        msv.addLayout(batt_row)
        self.alert_channel = QComboBox()
        self.alert_channel.setToolTip(
            "Channels configured on the companion device (filled after"
            " connecting). Pick the one that should receive the alerts,"
            " e.g. #weather.")
        msv.addWidget(self._field("Channel message", self.alert_channel))
        re_cap = QLabel("Re-alert")
        re_cap.setObjectName("Caption")
        msv.addWidget(re_cap)
        every_row = QHBoxLayout()
        every_row.setContentsMargins(0, 0, 0, 0)
        every_row.setSpacing(6)
        self.alert_every_radio = QRadioButton("every")
        every_row.addWidget(self.alert_every_radio)
        self.alert_every_h = QSpinBox()
        self.alert_every_h.setRange(1, 168)
        self.alert_every_h.setValue(6)
        self.alert_every_h.setSuffix(" h")
        every_row.addWidget(self.alert_every_h)
        every_row.addStretch(1)
        msv.addLayout(every_row)
        daily_row = QHBoxLayout()
        daily_row.setContentsMargins(0, 0, 0, 0)
        daily_row.setSpacing(6)
        self.alert_daily_radio = QRadioButton("daily at")
        daily_row.addWidget(self.alert_daily_radio)
        self.alert_daily_time = QTimeEdit(QTime(6, 35))
        self.alert_daily_time.setDisplayFormat("HH:mm")
        daily_row.addWidget(self.alert_daily_time)
        daily_row.addStretch(1)
        msv.addLayout(daily_row)

        # -- polling -----------------------------------------------------------
        msv.addWidget(side_cap("POLLING"))
        self.btn_poll_sel = QPushButton("Poll selected")
        self.btn_poll_sel.setToolTip(
            "Query the selected repeater right now — up to 6 tries, progress"
            " in the status bar.")
        self.btn_poll_sel.setEnabled(False)
        self.btn_poll_marked = QPushButton("Poll marked")
        self.btn_poll_marked.setToolTip(
            "Query every repeater whose Poll checkbox is ticked — up to 6"
            " tries each.")
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setProperty("danger", True)
        self.btn_stop.setToolTip("Abort the running poll round.")
        msv.addWidget(self.btn_poll_sel)
        msv.addWidget(self.btn_poll_marked)
        msv.addWidget(self.btn_stop)
        self.auto_connect = QCheckBox("Auto-connect on start")
        self.auto_connect.setToolTip(
            "Restore the saved companion device and connect automatically"
            " when MeshMonCore starts.")
        msv.addWidget(self.auto_connect)
        self.poll_enabled = QCheckBox("Auto-poll marked repeaters")
        self.poll_enabled.setToolTip(
            "When enabled, every repeater whose Poll checkbox is ticked is"
            " queried automatically (guest login + status + telemetry) per"
            " the schedule, and every result is stored in the history"
            " database. When disabled, only manual polls collect data.")
        msv.addWidget(self.poll_enabled)
        # mode combo full width; the minutes box / at-hours line swap below it
        # (the pair doesn't fit side by side in this narrow column)
        self.poll_mode = QComboBox()
        self.poll_mode.addItems(["Every", "At hours"])
        self.poll_mode.setToolTip(
            "Every: poll on a fixed interval (the minutes box)."
            " At hours: poll once at each listed time of day, e.g."
            " 06:00, 18:00 (comma separated).")
        msv.addWidget(self.poll_mode)
        self.poll_minutes = QSpinBox()
        self.poll_minutes.setRange(1, 1440)
        self.poll_minutes.setValue(15)
        self.poll_minutes.setSuffix(" min")
        msv.addWidget(self.poll_minutes)
        self.poll_at_hours = QLineEdit()
        self.poll_at_hours.setPlaceholderText("06:00, 18:00")
        self.poll_at_hours.setVisible(False)   # shown in "At hours" mode
        msv.addWidget(self.poll_at_hours)
        self.path_hash = QComboBox()
        self.path_hash.addItems(
            ["Path hash: 1 byte", "Path hash: 2 bytes", "Path hash: 3 bytes"])
        self.path_hash.setToolTip(
            "Bytes per hop used when building a custom path to a repeater"
            " (path hash mode 0/1/2). Saved and reused every time. 1 byte"
            " works for direct neighbours; use 2-3 bytes for distant"
            " repeaters behind other hops.")
        msv.addWidget(self.path_hash)
        msv.addStretch(1)
        mid_scroll.setWidget(mid)
        sv.addWidget(mid_scroll, 1)

        # scrolling the sidebar must never change a value: wheel events over
        # combo/spin/time widgets are swallowed and scroll the sections area
        self._wheel_guard = WheelGuard(mid_scroll)
        for cls in (QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit):
            for w in mid.findChildren(cls):
                w.installEventFilter(self._wheel_guard)

        self.lbl_conn = QLabel("○ not connected")
        self.lbl_conn.setObjectName("Subtle")
        self.lbl_conn.setAlignment(Qt.AlignHCenter)
        self.lbl_conn.setWordWrap(True)
        sv.addWidget(self.lbl_conn)
        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setProperty("accent", True)
        sv.addWidget(self.btn_connect)
        outer.addWidget(side)

        # ---- pages ----------------------------------------------------------
        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)

        # ===== page: Dashboard (first in the sidebar) =======================
        dash_scroll = QScrollArea()
        dash_scroll.setWidgetResizable(True)
        dash_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        dash_page = QWidget()
        dl = QVBoxLayout(dash_page)
        dl.setContentsMargins(14, 12, 14, 10)
        dl.setSpacing(10)

        dash_head = QHBoxLayout()
        dh_v = QVBoxLayout()
        dh_v.setSpacing(0)
        dh_t = QLabel("Top 10 Repeaters")
        dh_t.setObjectName("CardTitle")
        dh_t.setFont(QFont("DejaVu Sans", 14, QFont.Weight.Bold))
        dh_c = QLabel("Key metrics for the most active repeaters in your"
                      " mesh network.")
        dh_c.setObjectName("Caption")
        dh_v.addWidget(dh_t)
        dh_v.addWidget(dh_c)
        dash_head.addLayout(dh_v)
        dash_head.addStretch(1)
        self.dash_range = QComboBox()
        for lbl, _sec in DASH_RANGES:
            self.dash_range.addItem(lbl)
        self.dash_range.setToolTip("Time window for the dashboard statistics.")
        dash_head.addWidget(self.dash_range)
        self.btn_dash_customize = QPushButton("Customize…")
        dash_head.addWidget(self.btn_dash_customize)
        dl.addLayout(dash_head)

        self.dash_tiles = QHBoxLayout()
        self.dash_tiles.setSpacing(10)
        dl.addLayout(self.dash_tiles)

        table_card, tcl = self._card("⚑", "Top 10 Repeaters")
        dl.addWidget(table_card)
        self.dash_table = QTableWidget(0, 3)
        self.dash_table.verticalHeader().setVisible(False)
        self.dash_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.dash_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.dash_table.setAlternatingRowColors(True)
        self.dash_table.horizontalHeader().setStretchLastSection(False)
        self.dash_table.setMinimumHeight(340)
        tcl.addWidget(self.dash_table)
        more_row = QHBoxLayout()
        more_row.addStretch(1)
        self.btn_dash_more = QPushButton("▼ Show more")
        self.btn_dash_more.setProperty("ghost", True)
        self.btn_dash_more.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_dash_more.setToolTip(
            "Expand the table to every polled repeater instead of just"
            " the top ones.")
        self.btn_dash_more.setVisible(False)
        self.btn_dash_more.clicked.connect(self._dash_toggle_more)
        more_row.addWidget(self.btn_dash_more)
        more_row.addStretch(1)
        tcl.addLayout(more_row)

        self.dash_charts = QGridLayout()
        self.dash_charts.setHorizontalSpacing(10)
        self.dash_charts.setVerticalSpacing(10)
        charts_card, chl = self._card("◔", "Per-metric history")
        chl.addLayout(self.dash_charts)
        dl.addWidget(charts_card)
        dl.addStretch(1)
        dash_scroll.setWidget(dash_page)
        self.pages.addWidget(dash_scroll)          # PAGE_DASH (index 0)

        # ===== page 0: Overview =============================================
        ov = QWidget()
        ov_l = QVBoxLayout(ov)
        ov_l.setContentsMargins(14, 12, 14, 8)
        ov_l.setSpacing(10)
        # vertical stack: Repeaters card on top, History below (the wide
        # 13-column table reads better with the full page width)
        split = QSplitter(Qt.Vertical)

        # -- left column: repeaters table ---------------------------------------
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(10)

        # repeaters card
        rep_card, rl = self._card("⌖", "Repeaters")
        ll.addWidget(rep_card, 1)
        rep_head = QHBoxLayout()
        self.lbl_rep_count = QLabel("0 total")
        self.lbl_rep_count.setObjectName("Subtle")
        rep_head.addWidget(self.lbl_rep_count)
        rep_head.addStretch(1)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search by name or public key…")
        self.search_edit.setClearButtonEnabled(True)
        # size the box from its own placeholder text (ink + frame padding +
        # the clear button): a fixed 260 px clipped the hint to "Search by
        # name or public…", and any other hardcoded width would clip again
        # whenever the font or DPI changes
        self.search_edit.ensurePolished()
        self.search_edit.setFixedWidth(
            self.search_edit.fontMetrics().horizontalAdvance(
                self.search_edit.placeholderText()) + 64)
        self.search_edit.setToolTip(
            "Filter the table live by repeater name or public key prefix.")
        rep_head.addWidget(self.search_edit)
        rl.addLayout(rep_head)

        self.table = QTableWidget(0, 13)
        self.table.setHorizontalHeaderLabels(
            ["", "", "Name", "Public key", "Hops", "⚡", "Batt (V)",
             "Temp (°C)", "RSSI", "SNR", "Noise", "Util %", "Last poll"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.setTextElideMode(Qt.ElideNone)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        hdr.setStretchLastSection(False)
        for col in (COL_CHK, COL_STAT, COL_HOPS, COL_VOK, COL_BATT, COL_TEMP,
                    COL_RSSI, COL_SNR, COL_NOISE, COL_UTIL, COL_LAST):
            self.table.horizontalHeaderItem(col).setTextAlignment(
                Qt.AlignHCenter | Qt.AlignVCenter)
        self.table.verticalHeader().setDefaultSectionSize(30)
        rl.addWidget(self.table, 1)

        mark_row = QHBoxLayout()
        self.btn_mark_all = QPushButton("Select all")
        self.btn_mark_all.setToolTip("Tick the Poll checkbox on every repeater.")
        self.btn_mark_none = QPushButton("Deselect all")
        self.btn_mark_none.setToolTip("Clear the Poll checkbox on every repeater.")
        self.btn_mark_delete = QPushButton("Delete marked")
        self.btn_mark_delete.setProperty("danger", True)
        self.btn_mark_delete.setToolTip(
            "Remove every repeater whose Poll checkbox is ticked from the"
            " list — including its history. Asks for confirmation first;"
            " the companion's contact list is not touched (a re-scan"
            " re-registers them).")
        self.btn_path = QPushButton("Path…")
        self.btn_path.setToolTip(
            "View the route used to reach this repeater and set a custom path"
            " (choose intermediate repeaters from the contact list).")
        mark_row.addWidget(self.btn_mark_all)
        mark_row.addWidget(self.btn_mark_none)
        mark_row.addWidget(self.btn_mark_delete)
        mark_row.addWidget(self.btn_path)
        mark_row.addStretch(1)
        self.pw_edit = QLineEdit()
        self.pw_edit.setPlaceholderText("guest password — saved per repeater")
        self.pw_btn = QPushButton("Save login")
        self.pw_btn.setProperty("accent", True)
        self.lbl_pw = QLabel("Guest password: (select a repeater)")
        self.lbl_pw.setObjectName("Caption")
        mark_row.addWidget(self.lbl_pw)
        mark_row.addWidget(self.pw_edit, 1)
        mark_row.addWidget(self.pw_btn)
        rl.addLayout(mark_row)

        split.addWidget(left)

        # -- stat tiles: full-width row above the splitter ---------------------
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        t1, self.tile_name, self.tile_name_sub, _ = self._tile("Selected repeater")
        t2, self.tile_poll, self.tile_poll_sub, _ = self._tile("Last poll")
        t3, self.tile_batt, self.tile_batt_sub, self.tile_batt_bar = \
            self._tile("Battery (V)", bar_role="tileBarBat")
        t4, self.tile_temp, self.tile_temp_sub, self.tile_temp_bar = \
            self._tile("Temperature (°C)", bar_role="tileBarTemp")
        for t in (t1, t2, t3, t4):
            tiles.addWidget(t, 1)
        ov_l.addLayout(tiles)

        # -- bottom pane: history chart ----------------------------------------
        right = QWidget()
        rr = QVBoxLayout(right)
        rr.setContentsMargins(0, 0, 0, 0)
        rr.setSpacing(10)

        chart_card, ch = self._card("◔", "History")
        rr.addWidget(chart_card, 1)
        mrow2 = QHBoxLayout()
        self.metric_sel = MultiSelectCombo("select metrics")
        for m in METRIC_ORDER:
            self.metric_sel.addCheckItem(
                METRIC_LABELS[m][0], m,
                checked=m in ("battery_v", "temperature_c"))
        self.metric_sel.setToolTip(
            "Tick every metric you want on this chart; untick to remove it."
            " The selection is remembered between launches.")
        self.range_sel = QComboBox()
        self.range_sel.addItems(["1 h", "6 h", "24 h", "3 d", "7 d", "all"])
        self.range_sel.setCurrentIndex(4)   # 7d default (6h was too short)
        mrow2.addWidget(self._field("Metric", self.metric_sel))
        mrow2.addWidget(self._field("Range", self.range_sel))
        mrow2.addStretch(1)
        self.lbl_graph_title = QLabel("select a repeater")
        self.lbl_graph_title.setObjectName("Subtle")
        mrow2.addWidget(self.lbl_graph_title)
        ch.addLayout(mrow2)
        self.lbl_hint = QLabel("")
        self.lbl_hint.setObjectName("PlotHint")
        self.lbl_hint.hide()          # shown only when the range has no data
        ch.addWidget(self.lbl_hint)
        self.plot = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")})
        self.plot.showGrid(x=True, y=True, alpha=0.18)
        self.plot.addLegend(offset=(10, 10))
        self.plot.setBackground("#0d1522")
        ch.addWidget(self.plot, 1)
        split.addWidget(right)
        split.setSizes([max(380, int(self.height() * 0.52)),
                        max(300, int(self.height() * 0.38))])
        ov_l.addWidget(split, 1)
        self.pages.addWidget(ov)

        # ===== page 1: Map ==================================================
        map_page = QWidget()
        mp = QVBoxLayout(map_page)
        mp.setContentsMargins(14, 12, 14, 10)
        map_card, ml = self._card("⌖", "Map")
        mp.addWidget(map_card, 1)
        # basemap styles + link-strength legend
        brow = QHBoxLayout()
        self.basemap_buttons = {}
        for style, label in (("dark", "Dark"), ("satellite", "Satellite"),
                             ("osm", "OSM"), ("3d", "3D")):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(style == "dark")
            b.clicked.connect(lambda _c=False, st=style: self._set_basemap(st))
            self.basemap_buttons[style] = b
            brow.addWidget(b)
        brow.addStretch(1)
        legend = QLabel(
            "<span style='color:#2fd08c;'>●</span> Strong&nbsp;&nbsp;&nbsp;"
            "<span style='color:#4c8dff;'>●</span> Good&nbsp;&nbsp;&nbsp;"
            "<span style='color:#f5a623;'>●</span> Weak&nbsp;&nbsp;&nbsp;"
            "<span style='color:#5b6878;'>●</span> No data")
        legend.setObjectName("Caption")
        legend.setToolTip(
            "Link-line colors: the last RSSI your companion measured for"
            " each repeater (strong ≥ −85 dBm, good ≥ −100 dBm, weak below).")
        brow.addWidget(legend)
        ml.addLayout(brow)
        mrow = QHBoxLayout()
        self.btn_map_center = QPushButton("Center on selection")
        self.btn_map_center.setEnabled(False)
        self.btn_map_center.setToolTip(
            "Center the map on the repeater selected in the table.")
        self.btn_map_zoom_in = QPushButton("+")
        self.btn_map_zoom_in.setToolTip("Zoom in.")
        self.btn_map_zoom_out = QPushButton("−")
        self.btn_map_zoom_out.setToolTip("Zoom out.")
        mrow.addWidget(self.btn_map_center)
        mrow.addWidget(self.btn_map_zoom_in)
        mrow.addWidget(self.btn_map_zoom_out)
        mrow.addStretch(1)
        self.lbl_map_attr = QLabel(BASEMAPS["dark"][1])
        self.lbl_map_attr.setObjectName("Caption")
        mrow.addWidget(self.lbl_map_attr)
        ml.addLayout(mrow)
        self.map_view = SlippyMap(
            cache_dir=os.path.join(self._db_dir, "maptiles"))
        self._map_centered = False   # False -> first fix centers the map
        ml.addWidget(self.map_view, 1)
        self.pages.addWidget(map_page)

        # ===== page 2: Log ==================================================
        log_page = QWidget()
        lp = QVBoxLayout(log_page)
        lp.setContentsMargins(14, 12, 14, 10)
        log_card, lg = self._card("☰", "Log")
        lp.addWidget(log_card, 1)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(600)
        f = QFont("Monospace")
        f.setStyleHint(QFont.StyleHint.TypeWriter)
        self.log_view.setFont(f)
        lg.addWidget(self.log_view)
        self.pages.addWidget(log_page)

        # ===== page 3: Settings =============================================
        set_page = QWidget()
        stl = QVBoxLayout(set_page)
        stl.setContentsMargins(14, 12, 14, 10)
        stl.setAlignment(Qt.AlignTop)
        src_card, sc = self._card("⌖", "Repeater sources")
        stl.addWidget(src_card)
        cap = QLabel(
            "Register repeaters the companion has heard, add one manually by"
            " its public key, or clean up entries the companion no longer"
            " knows (their history is deleted too).")
        cap.setObjectName("Caption")
        cap.setWordWrap(True)
        sc.addWidget(cap)
        srow = QHBoxLayout()
        self.btn_scan = QPushButton("Register repeaters from contacts")
        self.btn_add_manual = QPushButton("Add by pubkey…")
        self.btn_cleanup = QPushButton("Clean up list")
        self.btn_cleanup.setEnabled(False)
        self.btn_cleanup.setToolTip(
            "Remove repeaters the companion no longer knows (evicted from its"
            " full contact list, or never heard). Their history is deleted"
            " too. A confirmation dialog lists everything before removal.")
        srow.addWidget(self.btn_scan)
        srow.addWidget(self.btn_add_manual)
        srow.addWidget(self.btn_cleanup)
        srow.addStretch(1)
        sc.addLayout(srow)
        mcap = QLabel(
            "Export the repeaters marked for polling (with their last known"
            " positions) to a JSON file — e.g. to mark the same set on"
            " another PC — or import a file exported earlier. Importing"
            " marks the listed repeaters; unknown ones are created.")
        mcap.setObjectName("Caption")
        mcap.setWordWrap(True)
        sc.addWidget(mcap)
        mrow = QHBoxLayout()
        self.btn_export_marked = QPushButton("Export Marked Repeaters")
        self.btn_import_marked = QPushButton("Import Marked Repeaters")
        mrow.addWidget(self.btn_export_marked)
        mrow.addWidget(self.btn_import_marked)
        mrow.addStretch(1)
        sc.addLayout(mrow)
        paths_card, pc = self._card("⇄", "Custom paths")
        stl.addWidget(paths_card)
        pcap = QLabel(
            "Export the custom paths you've assigned to repeaters (primary"
            " and secondary) to a JSON file — e.g. to set them up again on"
            " another PC — or import a file exported earlier. Importing sets"
            " the stored paths and pushes them to the companion if"
            " connected; unknown repeaters are created.")
        pcap.setObjectName("Caption")
        pcap.setWordWrap(True)
        pc.addWidget(pcap)
        prow = QHBoxLayout()
        self.btn_export_paths = QPushButton("Export Custom Paths")
        self.btn_import_paths = QPushButton("Import Custom Paths")
        prow.addWidget(self.btn_export_paths)
        prow.addWidget(self.btn_import_paths)
        prow.addStretch(1)
        pc.addLayout(prow)
        about_card, ab = self._card("⚙", "About")
        stl.addWidget(about_card)
        ab.addWidget(QLabel(
            f"MeshMonCore v{__version__} — MeshCore repeater monitor.\n"
            f"Data folder: {self._db_dir}"))
        self.pages.addWidget(set_page)

        self.setStatusBar(QStatusBar())
        self.setCentralWidget(root)

        # wiring
        self.btn_connect.clicked.connect(self._toggle_connection)
        self.conn_type.currentIndexChanged.connect(self._conn_type_changed)
        self.poll_mode.currentIndexChanged.connect(self._poll_mode_changed)
        self.poll_at_hours.editingFinished.connect(self._push_poll_schedule)
        self.poll_minutes.valueChanged.connect(self._push_poll_schedule)
        self.poll_enabled.toggled.connect(self._poll_toggled)
        self.btn_poll_sel.clicked.connect(self._poll_selected)
        self.btn_poll_marked.clicked.connect(self._poll_marked)
        self.btn_stop.clicked.connect(self._stop_poll)
        self.btn_path.clicked.connect(self._edit_path)
        self.btn_scan.clicked.connect(self._scan_contacts)
        self.btn_add_manual.clicked.connect(self._add_manual)
        self.btn_cleanup.clicked.connect(self._cleanup_list)
        self.btn_export_paths.clicked.connect(self._export_custom_paths)
        self.btn_import_paths.clicked.connect(self._import_custom_paths)
        self.btn_export_marked.clicked.connect(self._export_marked)
        self.btn_import_marked.clicked.connect(self._import_marked)
        self.pw_btn.clicked.connect(self._save_pw)
        self.btn_mark_all.clicked.connect(lambda: self._mark_all(True))
        self.btn_mark_none.clicked.connect(lambda: self._mark_all(False))
        self.btn_mark_delete.clicked.connect(self._delete_marked)
        # metric change fully rebuilds the graph so the selection shows at once
        self.metric_sel.connect_model(self._chart_metrics_changed)
        self.metric_sel.selection_changed.connect(self._chart_metrics_changed)
        self.range_sel.currentTextChanged.connect(lambda _t: self._set_range())
        # hover crosshair: live readout of the sample under the mouse
        self.plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        # alerts
        self.btn_alert_enabled.toggled.connect(self._alert_enabled_toggled)
        self.alert_low.valueChanged.connect(self._alert_settings_changed)
        self.alert_high.valueChanged.connect(self._alert_settings_changed)
        self.alert_low_v.valueChanged.connect(self._alert_settings_changed)
        self.alert_high_v.valueChanged.connect(self._alert_settings_changed)
        self.alert_every_radio.toggled.connect(self._alert_schedule_changed)
        self.alert_daily_radio.toggled.connect(self._alert_schedule_changed)
        self.alert_daily_time.timeChanged.connect(self._push_alert_schedule)
        # map
        self.table.itemSelectionChanged.connect(self._on_row_selected)
        self.table.itemChanged.connect(self._on_item_changed)
        self.pages.currentChanged.connect(
            lambda _idx: self._refresh_dashboard())
        self.search_edit.textChanged.connect(self._on_search)
        self.dash_table.horizontalHeader().sortIndicatorChanged.connect(
            lambda col, order: setattr(self, "_dash_sort", (col, order)))
        self.dash_range.currentIndexChanged.connect(self._dash_range_changed)
        self.btn_dash_customize.clicked.connect(self._dash_customize)
        self.map_view.marker_clicked.connect(self._on_marker_clicked)
        self.btn_map_center.clicked.connect(self._center_map_on_selection)
        self.btn_map_zoom_in.clicked.connect(
            lambda: self.map_view.set_zoom_step(1))
        self.btn_map_zoom_out.clicked.connect(
            lambda: self.map_view.set_zoom_step(-1))

    # ------------------------------------------------------------- settings
    def _load_settings(self):
        self._loading = True   # block saves triggered mid-load
        try:
            s = _settings()
            self.conn_type.setCurrentIndex(int(s.value("conn_type", 0)))  # 0 = Serial
            self.conn_host.setText(s.value("tcp_host", "", str))
            self.conn_port.setValue(int(s.value("tcp_port", 5000)))
            self.conn_serial.setText(s.value("serial_path", "/dev/ttyACM0", str))
            self.conn_baud.setValue(int(s.value("serial_baud", 115200)))
            self.poll_minutes.setValue(int(s.value("poll_minutes", 15)))
            self.poll_enabled.setChecked(s.value("poll_enabled", "0") == "1")
            self.auto_connect.setChecked(s.value("auto_connect", "1") == "1")
            self.path_hash.setCurrentIndex(int(s.value("path_hash", 0)))
            self.poll_mode.setCurrentIndex(int(s.value("poll_mode", 0)))
            self.poll_at_hours.setText(s.value("poll_at_hours", "06:00, 18:00", str))
            self.metric_sel.set_checked_data(
                (s.value("chart_metrics", "battery_v,temperature_c", str)).split(","))
            self.alert_low.setValue(int(s.value("alert_low", -15)))
            self.alert_high.setValue(int(s.value("alert_high", 60)))
            self.alert_low_v.setValue(float(s.value("alert_low_v", 3.30)))
            self.alert_high_v.setValue(float(s.value("alert_high_v", 4.30)))
            self.alert_every_h.setValue(int(s.value("alert_every_h", 6)))
            daily = s.value("alert_schedule_mode", "every")
            (self.alert_every_radio if daily == "every"
             else self.alert_daily_radio).setChecked(True)
            hh, mm = (s.value("alert_daily_hm", "06:35", str)).split(":")
            self.alert_daily_time.setTime(QTime(int(hh), int(mm)))
            saved_m = (s.value(
                "dash_metrics", "battery_v,temperature_c,rssi,snr,noise_floor,"
                "uptime_s,util_tx,util_rx", str)).split(",")
            if saved_m == ["battery_v", "temperature_c", "rssi", "snr",
                           "noise_floor", "uptime_s"]:
                saved_m += ["util_tx", "util_rx"]   # pre-utilization default
            self._dash_metrics = saved_m
            self._dash_rank = s.value("dash_rank", "last_heard", str)
            self._dash_range_s = int(s.value("dash_range_s", 24 * 3600))
            self._dash_n = int(s.value("dash_n", 10))
            self.dash_range.setCurrentIndex(
                next((i for i, (_l, sec) in enumerate(DASH_RANGES)
                      if sec == self._dash_range_s), 0))
            self._push_poll_schedule()
            self._push_alert_schedule()
            self._conn_type_changed(self.conn_type.currentIndex())
            self._poll_mode_changed(self.poll_mode.currentIndex())
            # restore the last map view (saved on every settings save)
            self.map_view.set_zoom(int(s.value("map_zoom", 10)))
            self.map_view.set_center(float(s.value("map_lat", 0.0)),
                                     float(s.value("map_lon", 0.0)))
            from PySide6.QtCore import QByteArray
            geom = s.value("win_geom")
            if isinstance(geom, (QByteArray, bytes)) and geom:
                try:
                    self.restoreGeometry(geom)
                except Exception:  # noqa: BLE001
                    pass
            # a geometry saved on a bigger display (or the pre-persistence
            # default) can overflow a small screen — keep it inside, and start
            # on whichever monitor the mouse is on
            self._clamp_to_screen(self._mouse_screen())
        finally:
            self._loading = False

    def _mouse_screen(self):
        """The screen the mouse cursor is currently on (the window's screen
        as fallback) — multi-monitor: MeshMonCore starts where the mouse is."""
        scr = QGuiApplication.screenAt(QCursor.pos())
        return scr or self.screen() or QGuiApplication.primaryScreen()

    def _clamp_to_screen(self, scr=None):
        """Shrink/move the window so it sits fully inside `scr`'s available
        area (excludes taskbars/panels). A window entirely off that screen
        is centered on it instead of being pushed to an edge."""
        scr = scr or self.screen() or QGuiApplication.primaryScreen()
        avail = scr.availableGeometry()
        w = min(self.width(), avail.width())
        h = min(self.height(), avail.height())
        geo = QRect(self.x(), self.y(), self.width(), self.height())
        if not geo.intersects(avail):
            x = avail.left() + (avail.width() - w) // 2
            y = avail.top() + (avail.height() - h) // 2
        else:
            x = min(max(self.x(), avail.left()), avail.right() - w + 1)
            y = min(max(self.y(), avail.top()), avail.bottom() - h + 1)
        self.setGeometry(x, y, w, h)

    def _save_settings(self):
        if getattr(self, "_loading", False):
            return   # a save triggered mid-load would write half-loaded values
        s = _settings()
        s.setValue("win_geom", self.saveGeometry())
        s.setValue("conn_type", self.conn_type.currentIndex())
        s.setValue("tcp_host", self.conn_host.text())
        s.setValue("tcp_port", self.conn_port.value())
        s.setValue("serial_path", self.conn_serial.text())
        s.setValue("serial_baud", self.conn_baud.value())
        s.setValue("poll_minutes", self.poll_minutes.value())
        s.setValue("poll_mode", self.poll_mode.currentIndex())
        s.setValue("poll_at_hours", self.poll_at_hours.text())
        s.setValue("chart_metrics", ",".join(self.metric_sel.checked_data()))
        s.setValue("poll_enabled", "1" if self.poll_enabled.isChecked() else "0")
        s.setValue("auto_connect", "1" if self.auto_connect.isChecked() else "0")
        s.setValue("dash_metrics", ",".join(self._dash_metrics))
        s.setValue("dash_rank", self._dash_rank)
        s.setValue("dash_range_s", self._dash_range_s)
        s.setValue("dash_n", self._dash_n)
        s.setValue("path_hash", self.path_hash.currentIndex())
        s.setValue("alert_low", self.alert_low.value())
        s.setValue("alert_high", self.alert_high.value())
        s.setValue("alert_low_v", self.alert_low_v.value())
        s.setValue("alert_high_v", self.alert_high_v.value())
        s.setValue("alert_every_h", self.alert_every_h.value())
        s.setValue("alert_schedule_mode",
                   "daily" if self.alert_daily_radio.isChecked() else "every")
        t = self.alert_daily_time.time()
        s.setValue("alert_daily_hm", f"{t.hour():02d}:{t.minute():02d}")
        cl = self.map_view.center_latlon()
        s.setValue("map_lat", cl[0])
        s.setValue("map_lon", cl[1])
        s.setValue("map_zoom", self.map_view._z)

    def _nav_icon(self, kind, color, size=22):
        """Rounded-line nav glyph (grid / home / map / log / gear) drawn at
        4x and displayed via devicePixelRatio, so it stays crisp on HiDPI."""
        S = 4
        pm = QPixmap(size * S, size * S)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color), 1.9 * S)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        u = size * S / 22.0

        def rect(x, y, w, h, r=2.2):
            p.drawRoundedRect(x * u, y * u, w * u, h * u, r * u, r * u)

        def line(x1, y1, x2, y2):
            p.drawLine(QPointF(x1 * u, y1 * u), QPointF(x2 * u, y2 * u))

        if kind == "grid":
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(color))
            for gx, gy in ((3.5, 3.5), (12.5, 3.5),
                           (3.5, 12.5), (12.5, 12.5)):
                rect(gx, gy, 6, 6, r=1.8)
        elif kind == "home":
            p.drawPolyline(QPolygonF([QPointF(3.2 * u, 11.2 * u),
                                      QPointF(11 * u, 4 * u),
                                      QPointF(18.8 * u, 11.2 * u)]))
            rect(5.6, 10.2, 10.8, 7.6, r=2.0)
        elif kind == "map":
            rect(3.5, 4.2, 15, 13.6, r=2.4)
            line(9.2, 4.2, 9.2, 17.8)
            line(14.6, 4.2, 14.6, 17.8)
        elif kind == "log":
            rect(5.2, 3.4, 11.6, 15.2, r=2.4)
            for y in (7.6, 11, 14.4):
                line(7.8, y, 14.2, y)
        elif kind == "gear":
            p.drawEllipse(QPointF(11 * u, 11 * u), 6.4 * u, 6.4 * u)
            for a in range(0, 360, 45):
                rad = math.radians(a)
                line(11 + 6.4 * math.cos(rad), 11 + 6.4 * math.sin(rad),
                     11 + 9.2 * math.cos(rad), 11 + 9.2 * math.sin(rad))
            p.drawEllipse(QPointF(11 * u, 11 * u), 2.2 * u, 2.2 * u)
        p.end()
        pm.setDevicePixelRatio(S)
        return pm

    def _on_nav(self, row):
        """Sidebar selection: switch pages and restyle the row pills."""
        pages = getattr(self, "pages", None)
        if pages is not None:            # still building the sidebar
            pages.setCurrentIndex(max(row, 0))
        for i, (pill, ic, chev, kind) in enumerate(self._nav_rows):
            sel = (i == row)
            pill.setProperty("sel", sel)
            pill.style().unpolish(pill)
            pill.style().polish(pill)
            ic.setPixmap(self._nav_icon(kind, "#ffffff" if sel
                                        else theme.TEXT_SUB))
            chev.setVisible(sel)

    def _auto_connect(self):
        if self.auto_connect.isChecked() and not self.radio.loop:
            self._toggle_connection()

    # -------------------------------------------------------------- actions
    def _conn_type_changed(self, idx):
        serial = idx == 0
        self.conn_serial_host.setVisible(serial)
        self.conn_serial_baud.setVisible(serial)
        self.conn_tcp_host.setVisible(not serial)
        self.conn_tcp_port.setVisible(not serial)
        self.conn_serial.setVisible(serial)
        self.conn_baud.setVisible(serial)
        self.conn_host.setVisible(not serial)
        self.conn_port.setVisible(not serial)

    def _poll_mode_changed(self, idx):
        hours = idx == 1
        self.poll_minutes.setVisible(not hours)
        self.poll_at_hours.setVisible(hours)

    def _toggle_connection(self):
        if self.radio.loop and not getattr(self, "_stopping", False):
            self._stopping = True
            self.radio.stop()
            self.lbl_conn.setText("○ disconnecting…")
            QTimer.singleShot(1500, lambda: setattr(self, "_stopping", False))
            self.btn_connect.setText("Connect")
            return
        if self.conn_type.currentIndex() == 0:
            path = self.conn_serial.text().strip()
            if not path:
                QMessageBox.warning(self, "MeshMonCore", "Enter the serial device.")
                return
            conf = ("serial", path, self.conn_baud.value())
        else:
            host = self.conn_host.text().strip()
            if not host:
                QMessageBox.warning(self, "MeshMonCore", "Enter the companion IP.")
                return
            conf = ("tcp", host, self.conn_port.value())
        self._save_settings()
        self.radio.poll_enabled = self.poll_enabled.isChecked()
        self._push_poll_schedule()
        self.radio.start(conf)
        self.btn_connect.setText("Disconnect")

    def _push_poll_schedule(self):
        self.radio.poll_interval = self.poll_minutes.value() * 60
        self.radio.poll_schedule_mode = ("hours" if self.poll_mode.currentIndex() == 1
                                         else "interval")
        minutes = []
        for part in self.poll_at_hours.text().split(","):
            part = part.strip()
            if not part:
                continue
            try:
                h, m = part.split(":")
                slot = int(h) * 60 + int(m)
            except ValueError:
                continue
            if 0 <= slot < 24 * 60:
                minutes.append(slot)
        self.radio.poll_hours_minutes = sorted(set(minutes))
        self.radio.wake_poll_schedule()   # schedule changed -> recompute now

    def _poll_toggled(self, on):
        self.radio.poll_enabled = on
        self._push_poll_schedule()
        self._save_settings()

    def _poll_selected(self):
        if not self._selected_pubkey:
            QMessageBox.information(self, "MeshMonCore", "Select a repeater first.")
            return
        self.radio.poll_now(self._selected_pubkey, tries=6)

    def _poll_marked(self):
        self.radio.poll_now(None, tries=6)

    def _stop_poll(self):
        self.radio.stop_poll()

    def _on_polling_started(self):
        self.btn_stop.setEnabled(True)
        self.btn_poll_sel.setEnabled(False)
        self.btn_poll_marked.setEnabled(False)

    def _on_polling_finished(self):
        self.btn_stop.setEnabled(False)
        self.btn_poll_sel.setEnabled(bool(self._selected_pubkey))
        self.btn_poll_marked.setEnabled(True)
        self.statusBar().clearMessage()
        self._refresh_views()

    # ------------------------------------------------------------- alerts
    def _alert_enabled_toggled(self, on):
        self.btn_alert_enabled.setText("Alerts on" if on else "Alerts off")
        # the green comes from #alertSwitch:checked (QSS) — the checked state
        # restyles itself on toggle, no manual repolish needed
        self._push_alert_schedule()
        self._alert_settings_changed()
        self.radio.wake_schedule()

    def _alert_settings_changed(self, _v=None):
        self.radio.alert_enabled = self.btn_alert_enabled.isChecked()
        self.radio.alert_low = float(self.alert_low.value())
        self.radio.alert_high = float(self.alert_high.value())
        self.radio.alert_low_v = float(self.alert_low_v.value())
        self.radio.alert_high_v = float(self.alert_high_v.value())
        idx = self.alert_channel.currentData()
        self.radio.alert_channel_idx = idx
        self._push_alert_schedule()
        self.radio.wake_schedule()      # attrs updated — now wake the loop
        self._save_settings()

    def _push_alert_schedule(self):
        self.radio.alert_schedule_mode = ("daily" if self.alert_daily_radio.isChecked()
                                          else "every")
        self.radio.alert_every_h = self.alert_every_h.value()
        t = self.alert_daily_time.time()
        self.radio.alert_daily_minutes = t.hour() * 60 + t.minute()

    def _alert_schedule_changed(self):
        self._push_alert_schedule()
        # gray out whichever side is inactive
        every = self.alert_every_radio.isChecked()
        self.alert_every_h.setEnabled(every)
        self.alert_daily_time.setEnabled(not every)

    def _apply_alert_settings_to_radio(self):
        self._alert_settings_changed()
        # auto-pick a channel whose name mentions weather, unless chosen
        if self.alert_channel.currentData() is None:
            for i in range(self.alert_channel.count()):
                if "weather" in self.alert_channel.itemText(i).lower():
                    self.alert_channel.setCurrentIndex(i)
                    break

    def _on_channels_changed(self, channels):
        cur = self.alert_channel.currentData()
        self.alert_channel.clear()
        for idx, nm in channels:
            self.alert_channel.addItem(f"#{nm}", idx)
        if cur is not None:
            i = self.alert_channel.findData(cur)
            if i >= 0:
                self.alert_channel.setCurrentIndex(i)
        elif channels:
            # first population: auto-pick a weather-named channel if present
            for i in range(self.alert_channel.count()):
                if "weather" in self.alert_channel.itemText(i).lower():
                    self.alert_channel.setCurrentIndex(i)
                    break
        self._alert_settings_changed()

    def _scan_contacts(self):
        n = self.radio.discover_repeaters()
        self._append_log(f"registered {n} repeaters from contacts")
        self._refresh_views()

    def _add_manual(self):
        from PySide6.QtWidgets import QInputDialog
        pk, ok = QInputDialog.getText(
            self, "Add repeater", "Full public key (64 hex chars):")
        if ok and pk.strip():
            self.store.upsert_repeater(pk.strip().lower())
            self._refresh_views()

    def _save_pw(self):
        if not self._selected_pubkey:
            return
        self.radio.save_guest_pw_and_login(
            self._selected_pubkey, self.pw_edit.text())
        self._append_log(f"guest password saved for {self._selected_pubkey[:12]}…")

    # ------------------------------------------------------ radio signals
    def _wire_radio(self):
        r = self.radio
        r.connected.connect(self._on_connected)
        r.contacts_changed.connect(lambda _c: self._refresh_views())
        r.poll_result.connect(self._on_poll_result)
        r.poll_failed.connect(
            lambda pk, err: self._append_log(f"POLL FAIL {pk[:12]}…: {err}"))
        r.log_line.connect(self._append_log)
        r.poll_progress.connect(self._on_poll_progress)
        r.polling_started.connect(self._on_polling_started)
        r.polling_finished.connect(self._on_polling_finished)
        r.channels_changed.connect(self._on_channels_changed)

    def _on_poll_progress(self, pubkey, attempt, tries):
        entry = self.store.repeater(pubkey)
        name = entry[1] if entry else pubkey[:12] + "…"
        self.statusBar().showMessage(
            f"Polling {name}: try {attempt}/{tries}…")
        self._append_log(f"poll {name}: try {attempt}/{tries}")

    def _on_connected(self, ok, detail):
        self._set_conn_chip(ok, detail)
        self.btn_cleanup.setEnabled(ok)
        if ok:
            self._append_log(f"connected ({detail})")
            self._apply_alert_settings_to_radio()
        # companion identity from the handshake's SELF_INFO
        info = (self.radio.mc.self_info
                if (ok and self.radio.mc and self.radio.mc.self_info) else {})
        lat, lon = info.get("adv_lat"), info.get("adv_lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) \
                and (abs(lat) > 0.01 or abs(lon) > 0.01):
            self._companion_pos = (float(lat), float(lon))
        else:
            self._companion_pos = None
        name = info.get("name")
        pk = info.get("public_key")
        if isinstance(pk, (bytes, bytearray)):
            pk = pk.hex()
        elif isinstance(pk, str):
            pk = pk.strip().lower()
            if pk.startswith("0x"):
                pk = pk[2:]
        self.conn_dev_name.setText(str(name) if name else "")
        self.conn_dev_pk.setText(pk if isinstance(pk, str) else "")
        self.conn_dev_name.setCursorPosition(0)
        self.conn_dev_pk.setCursorPosition(0)
        self.conn_dev_name.setVisible(bool(name))
        self.conn_dev_pk.setVisible(bool(pk))

    def _set_conn_chip(self, ok, text):
        color = theme.OK if ok else theme.DANGER
        self.lbl_conn.setText(
            f"<span style='color:{color};'>●</span>&nbsp;{text}")

    def _on_poll_result(self, pubkey, metrics):
        parts = []
        for k, v in sorted(metrics.items()):
            pretty = fmt_duration(v) if k == "uptime_s" else (
                f"{v:.2f}" if isinstance(v, float) else f"{v}")
            parts.append(f"{k}={pretty}")
        path = self.radio.contact_path(pubkey) or "direct"
        self._append_log(
            f"poll {pubkey[:12]}… via {path}: " + ", ".join(parts))
        self.statusBar().showMessage(
            f"Polled {pubkey[:12]}… via {path}", 5000)
        self._refresh_views()

    # ------------------------------------------------------------- views
    def _on_row_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, COL_PK)
        self._selected_pubkey = item.data(Qt.UserRole) if item else None
        if not self.btn_stop.isEnabled():          # not mid-poll
            self.btn_poll_sel.setEnabled(self._selected_pubkey is not None)
        entry = self.store.repeater(self._selected_pubkey) \
            if self._selected_pubkey else None
        name = entry[1] if entry else "—"
        pw = entry[2] if entry else ""
        has_fix = bool(self.store.positions().get(self._selected_pubkey))
        self.btn_map_center.setEnabled(has_fix)
        self.btn_map_center.setToolTip(
            "Center the map on this repeater." if has_fix
            else "No position known for this repeater (no advert with GPS"
                 " fix yet).")
        self.pw_edit.setText(pw or "")
        self._rebuild_graph()
        self._redraw()
        self._update_stat_tiles()

    def _selected(self):
        return self._selected_pubkey

    def _hops_text(self, pubkey):
        """Hops used to reach the repeater: live from the companion when
        connected, otherwise the stored custom path."""
        raw = self.radio.raw_path(pubkey) if self.radio.mc else None
        if raw:
            _hex, plen, _mode = raw
            if plen == 0:
                txt = "direct"                 # 0 hops, mesh-computed
            elif plen in (255, -1):
                txt = "unknown"                # no path learned yet
            else:
                txt = str(plen)
        else:
            stored_hex, mode = self.store.get_path(pubkey)
            txt = (str(len(stored_hex) // (2 * (mode + 1)))
                   if stored_hex else "—")
        sec_hex, _m2 = self.store.get_path2(pubkey)
        if sec_hex:
            txt += " · 2nd"
        return txt

    def _refresh_views(self):
        # Batched DB lookups (one query per metric) + a change signature: the
        # whole table rebuild is skipped when nothing visible changed.
        rows = self.store.repeaters()
        batt_all = self.store.latest_samples_all("battery_v")
        temp_all = self.store.latest_samples_all("temperature_c")
        rssi_all = self.store.latest_samples_all("rssi")
        snr_all = self.store.latest_samples_all("snr")
        noise_all = self.store.latest_samples_all("noise_floor")
        util_all = self.store.utilizations()
        st_all = self.store.last_poll_all("status")
        te_all = self.store.last_poll_all("telemetry")
        now = time.time()

        desired = []
        for (pubkey, name, _pw, _ls, last_err, poll_on,
             path_hex, _mode, voltage_correct) in rows:
            heard = (self.radio.find_contact(pubkey) is not None) \
                if self.radio.mc else False
            batt = batt_all.get(pubkey)
            temp = temp_all.get(pubkey)
            rssi = rssi_all.get(pubkey)
            snr = snr_all.get(pubkey)
            noise = noise_all.get(pubkey)
            util = util_all.get(pubkey)
            poll_ts = max([x[0] for x in (st_all.get(pubkey),
                                          te_all.get(pubkey)) if x] or [None])
            # status dot: data freshness (green fresh, amber stale, grey none)
            if poll_ts is None:
                dot, dot_col = "●", theme.TEXT_MUTE
            else:
                age = now - poll_ts
                if age <= 24 * 3600:
                    dot, dot_col = "●", theme.OK
                elif age <= 48 * 3600:
                    dot, dot_col = "●", theme.WARN
                else:
                    dot, dot_col = "●", theme.TEXT_MUTE
            desired.append((
                pubkey, bool(poll_on), bool(voltage_correct),
                dot, dot_col,
                name or pubkey[:12], self._hops_text(pubkey),
                f"{batt:.2f}" if batt is not None else "—",
                f"{temp:.1f}" if temp is not None else "—",
                f"{rssi:.0f}" if rssi is not None else "—",
                f"{snr:.1f}" if snr is not None else "—",
                f"{noise:.0f}" if noise is not None else "—",
                (f"{u[0]:.1f}" if (u := util_all.get(pubkey)) is not None
                 and u[0] is not None else "—"),
                (time.strftime("%Y-%m-%d %H:%M", time.localtime(poll_ts))
                 if poll_ts else "never"),
                bool(last_err), heard, bool(self.radio.mc),
            ))
        if desired == getattr(self, "_table_sig", None):
            self._update_stat_tiles()
            self._update_map_markers(batt_all, temp_all, rssi_all,
                                     st_all, te_all, noise_all)
            return                       # even when the table looks identical
        self._table_sig = desired

        bar = self.table.verticalScrollBar()
        scroll_pos = bar.value() if bar else 0
        current = self._selected_pubkey
        self.table.blockSignals(True)   # checkbox toggles handled in _on_item_changed
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            (pubkey, name, _pw, _ls, last_err, poll_on,
             path_hex, _mode, voltage_correct) = row
            d = desired[i]
            heard = d[-2]

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                         | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if poll_on else Qt.Unchecked)
            chk.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            chk.setToolTip("Poll this repeater automatically")
            self.table.setItem(i, COL_CHK, chk)

            vok = QTableWidgetItem()
            vok.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled
                         | Qt.ItemIsSelectable)
            vok.setCheckState(Qt.Checked if voltage_correct else Qt.Unchecked)
            vok.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            vok.setToolTip(
                "Tick if this repeater's voltage readings are trustworthy."
                " Unticked repeaters are EXCLUDED from voltage alert checks"
                " (temperature alerts still apply).")
            self.table.setItem(i, COL_VOK, vok)

            dot_item = QTableWidgetItem(d[3])
            dot_item.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            dot_item.setForeground(QColor(d[4]))
            dot_item.setToolTip(
                "Data freshness: green ≤ 24 h, amber ≤ 48 h, grey older"
                " or no data")
            self.table.setItem(i, COL_STAT, dot_item)

            cells = [
                (COL_NAME, d[5]),
                (COL_PK, pubkey[:12] + "…"),
                (COL_HOPS, d[6]),
                (COL_BATT, d[7]),
                (COL_TEMP, d[8]),
                (COL_RSSI, d[9]),
                (COL_SNR, d[10]),
                (COL_NOISE, d[11]),
                (COL_UTIL, d[12]),
                (COL_LAST, d[13]),
            ]
            centered = {COL_STAT, COL_HOPS, COL_BATT, COL_TEMP, COL_RSSI,
                        COL_SNR, COL_NOISE, COL_UTIL, COL_LAST}
            for col, text in cells:
                item = QTableWidgetItem(text)
                if col in centered:
                    item.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
                if col == COL_PK:
                    item.setData(Qt.UserRole, pubkey)
                self.table.setItem(i, col, item)
            util_it = self.table.item(i, COL_UTIL)
            if util_it is not None and util:
                util_it.setToolTip(
                    f"TX airtime utilization over the last poll interval:"
                    f" {util[0]:.1f} %\nRX: {util[1]:.1f} %")
            # not heard by the companion right now -> dim the row (dot stays)
            if self.radio.mc and not heard:
                for col in range(COL_NAME, COL_LAST + 1):
                    it = self.table.item(i, col)
                    if it:
                        it.setForeground(QColor(theme.TEXT_MUTE))
            if last_err:
                self.table.item(i, COL_LAST).setForeground(QColor(theme.DANGER))
        self.table.blockSignals(False)
        # one content-fit pass per actual rebuild (constant cost)
        self.table.resizeColumnsToContents()
        # resizeColumnsToContents assigns explicit widths and leaves the
        # Stretch Name column at its content width — the leftover viewport
        # then reads as a phantom empty column after Last poll; re-asserting
        # the mode makes Name expand into the leftover again immediately
        hdr_o = self.table.horizontalHeader()
        hdr_o.setSectionResizeMode(COL_NAME, QHeaderView.Interactive)
        hdr_o.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        # restore selection by pubkey + scroll position across refreshes
        if current:
            for i in range(self.table.rowCount()):
                it = self.table.item(i, COL_PK)
                if it and it.data(Qt.UserRole) == current:
                    self.table.selectRow(i)
                    break
        bar.setValue(min(scroll_pos, bar.maximum()))
        self.lbl_rep_count.setText(f"{len(rows)} total")
        self._apply_table_filter()
        self._update_stat_tiles()
        if self._selected_pubkey:
            self._redraw()
        self._update_map_markers(batt_all, temp_all, rssi_all,
                                 st_all, te_all, noise_all)
        if self.pages.currentIndex() == PAGE_DASH:
            self._refresh_dashboard()
        # first known position centers the map once (per session)
        if not self._map_centered and self.map_view._markers:
            pk, lat, lon, _n, *_x = self.map_view._markers[0]
            self.map_view.set_center(lat, lon)
            self._map_centered = True

    def _apply_table_filter(self):
        """Live row filter: name or public key contains the search text."""
        q = (getattr(self, "_table_filter", "") or "").strip().lower()
        for i in range(self.table.rowCount()):
            pk_it = self.table.item(i, COL_PK)
            nm_it = self.table.item(i, COL_NAME)
            hay = ((pk_it.data(Qt.UserRole) if pk_it else "") or "")
            txt = nm_it.text() if nm_it else ""
            hit = (not q) or (q in hay.lower()) or (q in txt.lower())
            self.table.setRowHidden(i, not hit)

    def _on_search(self, text):
        self._table_filter = text
        self._apply_table_filter()

    def _update_stat_tiles(self):
        """Selected-repeater stat tiles above the chart."""
        pk = self._selected_pubkey
        if not pk:
            self.tile_name.setText("—")
            self.tile_name_sub.setText("select a repeater in the table")
            self.tile_poll.setText("—")
            self.tile_poll_sub.setText("")
            self.tile_batt.setText("—")
            self.tile_batt_sub.setText("")
            self.tile_batt_bar.setValue(0)
            self.tile_temp.setText("—")
            self.tile_temp_sub.setText("")
            self.tile_temp_bar.setValue(0)
            return
        entry = self.store.repeater(pk)
        self.tile_name.setText(entry[1] if entry else pk[:12])
        self.tile_name.setToolTip(entry[1] if entry else pk)
        self.tile_name_sub.setText("hops: " + (self._hops_text(pk) or "?"))
        st = self.store.last_poll_all("status").get(pk)
        te = self.store.last_poll_all("telemetry").get(pk)
        poll_ts = max([x[0] for x in (st, te) if x] or [None])
        if poll_ts:
            self.tile_poll.setText(
                fmt_duration(max(0, int(time.time() - poll_ts))) + " ago")
            self.tile_poll_sub.setText(
                time.strftime("%H:%M:%S", time.localtime(poll_ts)))
        else:
            self.tile_poll.setText("never")
            self.tile_poll_sub.setText("")
        batt = self.store.latest_sample(pk, "battery_v")
        if batt is not None:
            self.tile_batt.setText(f"{batt:.2f}")
            pct = int(max(0.0, min(1.0, (batt - 3.0) / 1.2)) * 100)
            self.tile_batt_bar.setValue(pct)
            self.tile_batt_sub.setText(f"{pct}% of 3.0–4.2 V")
        else:
            self.tile_batt.setText("—")
            self.tile_batt_sub.setText("")
            self.tile_batt_bar.setValue(0)
        temp = self.store.latest_sample(pk, "temperature_c")
        if temp is not None:
            self.tile_temp.setText(f"{temp:.1f}")
            pct = int(max(0.0, min(1.0, (temp + 20.0) / 70.0)) * 100)
            self.tile_temp_bar.setValue(pct)
            self.tile_temp_sub.setText(f"{pct}% of −20…50 °C")
        else:
            self.tile_temp.setText("—")
            self.tile_temp_sub.setText("")
            self.tile_temp_bar.setValue(0)

    # ---- dashboard -------------------------------------------------------
    def _dash_range_changed(self, _idx):
        self._dash_range_s = DASH_RANGES[max(self.dash_range.currentIndex(),
                                             0)][1]
        self._dash_sig = None
        self._refresh_dashboard()

    # ---- custom path import / export -------------------------------------
    def _export_custom_paths(self):
        """Write every repeater's custom primary/secondary paths to a JSON
        file the user can carry to another PC."""
        entries = []
        for (pubkey, name, _pw, _ls, _le, _on,
             path_hex, path_mode, _vc) in self.store.repeaters():
            p2h, p2m = self.store.get_path2(pubkey)
            if not path_hex and not p2h:
                continue
            entries.append({
                "pubkey": pubkey,
                "name": name or "",
                "path_hex": path_hex,
                "path_mode": path_mode,
                "path2_hex": p2h,
                "path2_mode": p2m,
            })
        if not entries:
            QMessageBox.information(
                self, "MeshMonCore",
                "No custom paths to export — none of the repeaters has a"
                " custom primary or secondary path set.")
            return
        path, _f = QFileDialog.getSaveFileName(
            self, "Export custom paths",
            os.path.expanduser("~/meshmoncore_custom_paths.json"),
            "JSON (*.json)")
        if not path:
            return
        payload = {
            "type": "meshmon-custom-paths",
            "version": 1,
            "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "repeaters": entries,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "MeshMonCore", f"Export failed: {e}")
            return
        self._append_log(f"exported {len(entries)} custom path(s) to {path}")
        QMessageBox.information(
            self, "MeshMonCore",
            f"Exported {len(entries)} custom path(s) to {path}")

    def _import_custom_paths(self):
        """Read a custom-paths JSON exported by MeshMon. Entries for
        repeaters already in the list update their stored paths; unknown
        repeaters are created (paths apply on the next connect)."""
        path, _f = QFileDialog.getOpenFileName(
            self, "Import custom paths",
            os.path.expanduser("~/meshmoncore_custom_paths.json"),
            "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "MeshMonCore", f"Import failed: {e}")
            return
        if (not isinstance(payload, dict)
                or payload.get("type") != "meshmon-custom-paths"
                or not isinstance(payload.get("repeaters"), list)):
            QMessageBox.critical(self, "MeshMonCore",
                                 "That file is not a MeshMon custom-paths"
                                 " export.")
            return
        applied, created = 0, 0
        for e in payload["repeaters"]:
            pk = str(e.get("pubkey", "")).strip().lower()
            if len(pk) != 64:
                continue
            try:
                bytes.fromhex(pk)
            except ValueError:
                continue
            if self.store.repeater(pk) is None:
                self.store.upsert_repeater(pk, name=e.get("name") or None)
                created += 1
            ph, pm = str(e.get("path_hex", "")).strip(), \
                int(e.get("path_mode", 0) or 0)
            p2h, p2m = str(e.get("path2_hex", "")).strip(), \
                int(e.get("path2_mode", 0) or 0)
            self.store.set_path(pk, ph, pm)
            self.store.set_path2(pk, p2h, p2m)
            applied += 1
        # push the imported PRIMARY paths to the companion if connected
        if applied and self.radio.mc:
            self.radio.call_soon(self.radio._apply_stored_paths())
        self._append_log(
            f"imported custom paths: {applied} entr(ies), {created} new"
            " repeater(s) created")
        self._refresh_views()
        QMessageBox.information(
            self, "MeshMonCore",
            f"Imported {applied} custom path(s) ({created} new repeaters)."
            " Primary paths were pushed to the companion."
            if self.radio.mc else
            f"Imported {applied} custom path(s) ({created} new repeaters).")

    # ---- marked repeater import / export ---------------------------------
    def _export_marked(self):
        """Write the repeaters currently MARKED for polling (pubkey, name,
        last known position) to a JSON file — e.g. to mark the same set on
        another PC."""
        pos = self.store.positions()
        entries = [
            {"pubkey": pubkey,
             "name": name or "",
             "lat": pos[pubkey][0] if pubkey in pos else None,
             "lon": pos[pubkey][1] if pubkey in pos else None}
            for (pubkey, name, _pw, _ls, _le, on,
                 _ph, _pm, _vc) in self.store.repeaters() if on
        ]
        if not entries:
            QMessageBox.information(
                self, "MeshMonCore",
                "No marked repeaters to export — nothing is ticked for"
                " polling.")
            return
        path, _f = QFileDialog.getSaveFileName(
            self, "Export marked repeaters",
            os.path.expanduser("~/meshmoncore_marked_repeaters.json"),
            "JSON (*.json)")
        if not path:
            return
        payload = {
            "type": "meshmon-marked-repeaters",
            "version": 1,
            "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "repeaters": entries,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "MeshMonCore", f"Export failed: {e}")
            return
        self._append_log(f"exported {len(entries)} marked repeater(s)"
                         f" to {path}")
        QMessageBox.information(
            self, "MeshMonCore",
            f"Exported {len(entries)} marked repeater(s) to {path}")

    def _import_marked(self):
        """Mark every repeater listed in a marked-repeaters JSON exported by
        MeshMon. Unknown repeaters are created (with their exported name and
        position) and marked; marks are only ever ADDED — nothing already in
        the list is unmarked."""
        path, _f = QFileDialog.getOpenFileName(
            self, "Import marked repeaters",
            os.path.expanduser("~/meshmoncore_marked_repeaters.json"),
            "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "MeshMonCore", f"Import failed: {e}")
            return
        if (not isinstance(payload, dict)
                or payload.get("type") != "meshmon-marked-repeaters"
                or not isinstance(payload.get("repeaters"), list)):
            QMessageBox.critical(
                self, "MeshMonCore",
                "That file is not a MeshMon marked-repeaters export.")
            return
        applied, created, skipped = 0, 0, 0
        for e in payload["repeaters"]:
            pk = str(e.get("pubkey", "")).strip().lower()
            if len(pk) != 64:
                skipped += 1
                continue
            try:
                bytes.fromhex(pk)
            except ValueError:
                skipped += 1
                continue
            if self.store.repeater(pk) is None:
                self.store.upsert_repeater(pk, name=e.get("name") or None)
                created += 1
            try:
                lat, lon = e.get("lat"), e.get("lon")
                if lat is not None or lon is not None:
                    self.store.set_position(pk, lat or 0, lon or 0)
            except (TypeError, ValueError):
                pass
            self.store.set_poll_enabled(pk, True)
            applied += 1
        self._append_log(
            f"imported marked repeaters: {applied} marked"
            f" ({created} new repeater(s) created"
            + (f", {skipped} skipped" if skipped else "") + ")")
        self._refresh_views()
        QMessageBox.information(
            self, "MeshMonCore",
            f"Marked {applied} repeater(s) ({created} new)."
            + (f" {skipped} entr(ies) had an invalid public key and were"
               " skipped." if skipped else ""))

    def _dash_customize(self):
        """Pick which metrics appear on the dashboard, what ranks the top
        list, and how many repeaters to show."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Customize dashboard")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel("Metrics (tiles, table columns and charts):"))
        checks = {}
        grid = QGridLayout()
        for i, m in enumerate(METRIC_ORDER):
            cb = QCheckBox(METRIC_LABELS[m][0])
            cb.setChecked(m in self._dash_metrics)
            grid.addWidget(cb, i // 3, i % 3)
            checks[m] = cb
        v.addLayout(grid)
        row = QHBoxLayout()
        row.addWidget(QLabel("Rank top list by:"))
        rank = QComboBox()
        rank.addItem("Last heard", "last_heard")
        for m in METRIC_ORDER:
            rank.addItem("Highest " + METRIC_LABELS[m][0], m)
        rank.setCurrentIndex(max(0, rank.findData(self._dash_rank)))
        row.addWidget(rank)
        row.addWidget(QLabel("Show:"))
        n = QSpinBox()
        n.setRange(3, 25)
        n.setValue(self._dash_n)
        n.setSuffix(" repeaters")
        row.addWidget(n)
        row.addStretch(1)
        v.addLayout(row)
        h2 = QHBoxLayout()
        h2.addStretch(1)
        ok = QPushButton("Apply")
        ok.setProperty("accent", True)
        cancel = QPushButton("Cancel")
        h2.addWidget(ok)
        h2.addWidget(cancel)
        v.addLayout(h2)

        done = {"ok": False}

        def _apply():
            self._dash_metrics = [m for m in METRIC_ORDER
                                  if checks[m].isChecked()]
            if not self._dash_metrics:
                self._dash_metrics = ["battery_v"]
            self._dash_rank = rank.currentData()
            self._dash_n = n.value()
            done["ok"] = True
            dlg.accept()

        ok.clicked.connect(_apply)
        cancel.clicked.connect(dlg.reject)
        dlg.exec()
        if done["ok"]:
            self._dash_sig = None
            self._refresh_dashboard()

    def _dash_toggle_more(self):
        self._dash_expand = not self._dash_expand
        self._dash_sig = None
        self._refresh_dashboard()

    def _fit_dash_table_height(self):
        """Size the dashboard table to its exact content: a padded minimum
        height showed a sliver of an empty extra row under the last one; the
        horizontal scrollbar allowance only applies when that scrollbar is
        actually visible."""
        t = self.dash_table
        extra = (t.horizontalScrollBar().height()
                 if t.horizontalScrollBar().isVisible() else 0)
        t.setMinimumHeight(t.horizontalHeader().height()
                           + t.verticalHeader().defaultSectionSize()
                           * t.rowCount() + extra + 2)

    def _dash_top(self, since):
        """The dashboard's ranked repeaters (full list — the caller decides
        how many to show): ranked by the chosen metric's newest value (any
        age, descending), or by last heard. The range selector governs the
        charts and trends, not the top-list membership — a repeater with
        stale data still belongs to its rank."""
        rank_m = None if self._dash_rank == "last_heard" else self._dash_rank
        wanted = set(self._dash_metrics)
        if rank_m:
            wanted.add(rank_m)
        latest = {m: self.store.latest_with_ts(m) for m in wanted}
        rows = []
        for (pubkey, name, _pw, _ls, _le, _on, _ph, _pm, _vc) \
                in self.store.repeaters():
            last_ts = 0
            for m in self._dash_metrics:
                e = latest.get(m, {}).get(pubkey)
                if e and e[0] > last_ts:
                    last_ts = e[0]
            if last_ts == 0:
                continue                       # never reported anything
            if rank_m:
                e = latest[rank_m].get(pubkey)
                key = ((e[1] if e else float("-inf")), last_ts)
            else:
                key = last_ts
            rows.append((pubkey, name, last_ts, key))
        rows.sort(key=lambda r: r[3], reverse=True)
        return rows

    def _refresh_dashboard(self, force=False):
        """Rebuild the dashboard when its page is shown or its data changed."""
        if self.pages.currentIndex() != PAGE_DASH:
            return
        since = time.time() - self._dash_range_s
        metrics = self._dash_metrics
        max_ts = self.store.max_sample_ts(since)
        sig = (tuple(metrics), self._dash_rank, self._dash_range_s,
               self._dash_n, self._dash_expand, max_ts)
        if not force and sig == getattr(self, "_dash_sig", None):
            return
        self._dash_sig = sig

        top_all = self._dash_top(since)
        top = top_all if self._dash_expand else top_all[:self._dash_n]
        self.btn_dash_more.setVisible(
            self._dash_expand or len(top_all) > self._dash_n)
        self.btn_dash_more.setText(
            "▲ Show less" if self._dash_expand else "▼ Show more")
        pubkeys = [r[0] for r in top]

        # ---- KPI tiles: per metric, the LATEST KNOWN value of each top-10
        #      repeater (any age) averaged — so a repeater whose last poll
        #      was earlier still counts ("across top 10", not "across top
        #      however-many-polling-succeeded-in-the-window"). Trend vs the
        #      previous window's average. Battery readings outside the
        #      sanity window (< 2 V unpowered/invalid, > 4.3 V impossible
        #      for a single Li-ion cell) are ignored in the averages.
        while self.dash_tiles.count():
            it = self.dash_tiles.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        t_prev0 = since - self._dash_range_s
        for m in metrics:
            cur_all = self.store.latest_samples_all(m)
            prev_all = self.store.latest_samples_between(m, t_prev0, since)
            def _avg_ok(metric, v):
                """Sanity window for the averages; False excludes the value.
                Battery: < 2 V is unpowered/invalid, > 4.3 V is impossible
                for a single Li-ion cell."""
                if metric == "battery_v":
                    return 2.0 <= v <= 4.3
                return True

            cur_vals, pvals = [], []
            for pk in pubkeys:
                v = cur_all.get(pk)
                if v is not None and _avg_ok(m, v):
                    cur_vals.append(v)
                pv = prev_all.get(pk)
                if pv is not None and _avg_ok(m, pv):
                    pvals.append(pv)
            avg = (sum(cur_vals) / len(cur_vals)) if cur_vals else None
            pavg = (sum(pvals) / len(pvals)) if pvals else None
            f = QFrame()
            f.setObjectName("Tile")
            fv = QVBoxLayout(f)
            fv.setContentsMargins(12, 10, 12, 10)
            fv.setSpacing(3)
            cap = QLabel("Avg. " + METRIC_LABELS[m][0])
            cap.setObjectName("Caption")
            val = QLabel(_fmt_metric(m, avg) if avg is not None else "—")
            val.setObjectName("BigValue")
            sub = QLabel("across top {}".format(len(cur_vals)))
            sub.setObjectName("Subtle")
            sub2 = QLabel("")
            sub2.setObjectName("Subtle")
            if avg is not None and pavg not in (None, 0):
                d = (avg - pavg) / abs(pavg) * 100.0
                up = d > 0
                good = DASH_UP_GOOD.get(m)
                col = (theme.TEXT_MUTE if good is None
                       else (theme.OK if (up == good) else theme.DANGER))
                sub2.setText(
                    f"<span style='color:{col};'>"
                    f"{'↑' if up else '↓'} {abs(d):.1f}%</span>"
                    f" <span style='color:{theme.TEXT_MUTE};'>vs prev</span>")
            fv.addWidget(cap)
            fv.addWidget(val)
            fv.addWidget(sub)
            fv.addWidget(sub2)
            self.dash_tiles.addWidget(f, 1)

        # ---- ranked table with per-metric rows ------------------------------
        headers = ["#", "", "Name", "Public key"] \
            + [METRIC_LABELS[m][0] for m in metrics] + ["Last heard"]
        t = self.dash_table
        t.clear()
        # sorting must be OFF while the table is populated: with an active
        # sort column, every setItem on that column re-sorts mid-fill and
        # scrambles the rows (names, values and mini-bars all misalign)
        t.setSortingEnabled(False)
        t.setColumnCount(len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.setRowCount(len(top))

        latest_all = {m: self.store.latest_in_window(m, since)
                      for m in metrics}
        for i, (pubkey, name, last_ts, _key) in enumerate(top):
            it = DashItem(f"{i + 1}", i)      # rank position = sort key
            it.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            it.setForeground(QColor(theme.TEXT_SUB))
            t.setItem(i, 0, it)
            age = time.time() - last_ts
            dot_col = (theme.OK if age <= 24 * 3600
                       else (theme.WARN if age <= 48 * 3600
                             else theme.TEXT_MUTE))
            dot_it = DashItem("●", last_ts)   # sorts by data age
            dot_it.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            dot_it.setForeground(QColor(dot_col))
            dot_it.setToolTip(f"last sample {int(age) // 60} min ago")
            t.setItem(i, 1, dot_it)
            nm = DashItem(name or pubkey[:12] + "…",
                          (name or pubkey[:12]).lower())
            t.setItem(i, 2, nm)
            pk_it = DashItem(pubkey[:12] + "…", pubkey)
            pk_it.setForeground(QColor(theme.TEXT_SUB))
            t.setItem(i, 3, pk_it)
            col = 4
            for m in metrics:
                e = latest_all[m].get(pubkey)
                val = e[1] if e else None
                key = (float("-inf") if val is None else float(val))
                # value + colored mini-bar cell (the reference dashboard look)
                wrap = QWidget()
                wv = QVBoxLayout(wrap)
                wv.setContentsMargins(2, 1, 2, 1)
                wv.setSpacing(1)
                lab = QLabel(_fmt_metric(m, val))
                lab.setAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
                wv.addWidget(lab)
                if val is not None:
                    lo, hi, c = DASH_BARS.get(m, (None, None, "#4c8dff"))
                    if lo is None:
                        others = [latest_all[m].get(p)[1]
                                  for p in pubkeys
                                  if latest_all[m].get(p) is not None]
                        lo, hi = 0.0, max(others + [1.0])
                    bar = MiniBar()
                    bar.setRange(0, 100)
                    span = (hi - lo) or 1.0
                    bar.setValue(int(max(0.0, min(1.0,
                                 (val - lo) / span)) * 100))
                    bar.setTextVisible(False)
                    bar.setFixedHeight(5)
                    # Ignored horizontally: the bar fills whatever cell width
                    # it gets, but its (wide) style size hint must not inflate
                    # the column during resizeColumnsToContents — that squeezed
                    # the Last heard column into truncation
                    bar.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Fixed)
                    bar.setStyleSheet(
                        f"QProgressBar::chunk {{ background: {c};"
                        f" border-radius: 2px; }}")
                    wv.addWidget(bar)
                # invisible keyed item in the same cell (EMPTY text — the bar
                # widget paints the value) makes the column sortable by the
                # numeric value
                t.setItem(i, col, DashItem("", key))
                t.setCellWidget(i, col, wrap)
                col += 1
            heard = DashItem(
                time.strftime("%Y-%m-%d %H:%M", time.localtime(last_ts))
                if last_ts else "—", last_ts)
            heard.setTextAlignment(Qt.AlignHCenter | Qt.AlignVCenter)
            t.setItem(i, col, heard)
        # the sort indicator is hidden for the resize pass: with it shown,
        # resizeColumnsToContents adds its width (~21 px) to EVERY column,
        # inflating the sum past the viewport and forcing a horizontal
        # scrollbar (which reads as an empty extra row under the table)
        hdr_d = t.horizontalHeader()
        hdr_d.setSortIndicatorShown(False)
        t.resizeColumnsToContents()
        t.setSortingEnabled(True)
        sc, so = self._dash_sort
        if sc < t.columnCount():
            t.sortItems(sc, so)
            hdr_d.setSortIndicator(sc, so)   # re-shows the indicator
        # re-assert AFTER the resize pass: resizeColumnsToContents assigns
        # explicit widths to every column and kills the Stretch mode, which
        # left an empty gap on the right of the dashboard table. The Name
        # column stretches to absorb the leftover — Last heard stays at its
        # content width, so the date can never be truncated (a Stretch Last
        # heard was squeezed whenever the other columns' resize sum
        # approached the viewport).
        for cidx in range(t.columnCount()):
            if cidx == 2:      # Name
                continue
            hdr_d.setSectionResizeMode(cidx, QHeaderView.Interactive)
        hdr_d.setSectionResizeMode(2, QHeaderView.Stretch)
        # fit the table height exactly — deferred call below re-fits once the
        # layout pass has settled (scrollbar visibility is only reliable then)
        self._fit_dash_table_height()
        QTimer.singleShot(0, self._fit_dash_table_height)

        # ---- per-metric mini charts (one line per top repeater) -------------
        while self.dash_charts.count():
            it = self.dash_charts.takeAt(0)
            w = it.widget()
            if w:
                w.deleteLater()
        for mi, m in enumerate(metrics):
            is_uptime = m == "uptime_s"
            card, cv = self._card(
                "◔", METRIC_LABELS[m][0] + (" (days)" if is_uptime else ""))
            axis_items = {"bottom": pg.DateAxisItem(orientation="bottom")}
            if is_uptime:
                axis_items["left"] = UptimeDaysAxis(orientation="left")
            plot = pg.PlotWidget(axisItems=axis_items)
            plot.setBackground("#0d1522")
            plot.showGrid(x=True, y=True, alpha=0.18)
            plot.setMouseEnabled(False, False)
            series = self.store.series_since(m, since)
            legend_rows = ""
            for ri, (pubkey, name, last_ts, _key) in enumerate(top):
                pts = series.get(pubkey, [])
                if not pts:
                    continue
                # battery history drops the same bogus readings the Avg.
                # tile excludes (outside [2.0, 4.3] V) so the graph and the
                # tile agree; other metrics plot every sample
                if m == "battery_v":
                    pts = [p for p in pts if _avg_ok(m, p[1])]
                    if not pts:
                        continue
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                if is_uptime:
                    ys = [y / 86400.0 for y in ys]   # seconds -> days
                col = DASH_COLORS[ri % len(DASH_COLORS)]
                plot.plot(xs, ys, pen=pg.mkPen(QColor(col), width=1.5))
                e = latest_all[m].get(pubkey)
                legend_rows += (
                    f"<div style='margin:1px 0;'>"
                    f"<span style='color:{col};'>●</span> "
                    f"<span style='color:#e6edf6;'>{name or pubkey[:12]}</span>"
                    f" <span style='color:#8a97a8;'>"
                    f"{_fmt_metric(m, e[1]) if e else '—'}</span></div>")
            card.setMinimumHeight(250)
            body = QHBoxLayout()
            body.setSpacing(8)
            body.addWidget(plot, 1)
            if legend_rows:
                lg = QLabel(legend_rows)
                lg.setObjectName("Caption")
                lg.setFixedWidth(200)
                lg.setAlignment(Qt.AlignLeft | Qt.AlignTop)
                body.addWidget(lg)
            cv.addLayout(body, 1)
            self.dash_charts.addWidget(card, mi // 3, mi % 3)

    # ---- dashboard (end) --------------------------------------------------

    def _on_item_changed(self, item):
        # checkbox toggles persist immediately (poll + volt-ok columns)
        if item.column() not in (COL_CHK, COL_VOK):
            return
        pk_item = self.table.item(item.row(), COL_PK)
        if not pk_item:
            return
        pubkey = pk_item.data(Qt.UserRole)
        if not pubkey:
            return
        checked = item.checkState() == Qt.Checked
        if item.column() == COL_CHK:
            self.store.set_poll_enabled(pubkey, checked)
        else:
            self.store.set_voltage_correct(pubkey, checked)
        self._table_sig = None    # next timer tick re-syncs the table once

    def _mark_all(self, on):
        """Select all / deselect all: tick or clear every Poll checkbox."""
        self.table.blockSignals(True)
        self.store.set_poll_enabled_all(on)
        state = Qt.Checked if on else Qt.Unchecked
        for i in range(self.table.rowCount()):
            it = self.table.item(i, 0)
            if it:
                it.setCheckState(state)
        self.table.blockSignals(False)
        self._table_sig = None    # next timer tick re-syncs the table once
        self._append_log(f"{'selected' if on else 'deselected'} all repeaters"
                         " for polling")

    def _delete_marked(self):
        """Remove every MARKED repeater (Poll checkbox ticked) from the list —
        repeater row, samples, poll log and alert state — after a
        confirmation dialog that lists exactly what will go. The companion's
        contact list is not touched."""
        marked = [(pubkey, name) for (pubkey, name, _pw, _ls, _le, on,
                                     _ph, _pm, _vc) in self.store.repeaters()
                  if on]
        if not marked:
            QMessageBox.information(
                self, "MeshMonCore",
                "Nothing to delete — no repeater is marked for polling.")
            return
        box = QDialog(self)
        box.setWindowTitle("Delete marked repeaters")
        box.resize(520, 420)
        v = QVBoxLayout(box)
        lbl = QLabel(
            f"Delete {len(marked)} marked repeater(s) — their rows, samples,"
            " poll log and alert state? The companion's contact list is not"
            " touched; a re-scan re-registers them.")
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        lst = QListWidget()
        lst.setAlternatingRowColors(True)
        for pk, name in marked:
            lst.addItem(f"{name or pk[:12] + '…'}  —  {pk[:12]}…")
        lst.setMaximumHeight(240)      # scrolls internally beyond this
        v.addWidget(lst)
        h2 = QHBoxLayout()
        h2.addStretch(1)
        btn_delete = QPushButton(f"Delete {len(marked)}")
        btn_delete.setProperty("danger", True)
        btn_cancel = QPushButton("Cancel")
        h2.addWidget(btn_delete)
        h2.addWidget(btn_cancel)
        v.addLayout(h2)

        confirmed = {"ok": False}

        def _do_delete():
            confirmed["ok"] = True
            box.accept()

        btn_delete.clicked.connect(_do_delete)
        btn_cancel.clicked.connect(box.reject)
        box.exec()
        if not confirmed["ok"]:
            return
        for pk, _name in marked:
            self.store.forget_repeater(pk)
        if self._selected_pubkey and self.store.repeater(
                self._selected_pubkey) is None:
            self._selected_pubkey = None
        self._companion_pos = None   # (lat, lon) from the handshake, for links
        self._table_filter = ""
        self._dash_metrics = ["battery_v", "temperature_c", "rssi", "snr",
                              "noise_floor", "uptime_s", "util_tx",
                              "util_rx"]
        self._dash_rank = "last_heard"
        self._dash_range_s = 24 * 3600
        self._dash_n = 10
        self._dash_sig = None
        self._dash_sort = (0, Qt.SortOrder.AscendingOrder)   # (column, order)
        self._append_log(f"deleted {len(marked)} marked repeater(s)")
        self._refresh_views()

    def _chart_metrics_changed(self):
        self._save_settings()
        self._rebuild_graph()

    def _set_range(self):
        table = {"1 h": 3600, "6 h": 6 * 3600, "24 h": 24 * 3600,
                 "3 d": 3 * 86400, "7 d": 7 * 86400, "all": None}
        r = table[self.range_sel.currentText()]
        self._range_s = r
        self._redraw()

    def _rebuild_graph(self):
        """Full rebuild — called on repeater or metric change so the newly
        selected metric is plotted immediately."""
        self.plot.clear()
        self._plot_curves = {}
        self._plot_data = {}
        if not self._selected_pubkey:
            return
        entry = self.store.repeater(self._selected_pubkey)
        title = entry[1] if entry else (self._selected_pubkey[:12] + "…")
        # the user picks the exact set of metrics on the chart (checkable
        # combo); all checked metrics plot with equal weight
        metrics = self.metric_sel.checked_data()
        self.lbl_graph_title.setText(
            f"{title} — history ({len(metrics)} metric"
            f"{'s' if len(metrics) != 1 else ''} selected)")
        for m in metrics:
            label, unit, color = METRIC_LABELS.get(m, (m, "", "#CCCCCC"))
            curve = self.plot.plot(
                [], [],
                pen=pg.mkPen(QColor(color), width=2),
                name=label,
                symbol="o",
                symbolSize=6,
                symbolBrush=QColor(color),
                symbolPen=None)
            self._plot_curves[m] = (curve, label, unit, True)
        # hover crosshair + in-plot readout (re-created after clear())
        self.hover_vline = pg.InfiniteLine(
            angle=90, movable=False,
            pen=pg.mkPen("#8899aa", width=1, style=Qt.DashLine))
        self.hover_text = pg.TextItem("", anchor=(0, 1), color="#e6edf3",
                                      fill=pg.mkBrush(16, 21, 28, 210))
        self.hover_text.setZValue(30)
        self.hover_text.hide()
        self.hover_vline.hide()
        self.plot.addItem(self.hover_vline)
        self.plot.addItem(self.hover_text)
        self._redraw()

    def _fmt_value(self, metric, value):
        if metric == "uptime_s":
            return fmt_duration(value)
        _label, unit, _c = METRIC_LABELS.get(metric, (metric, "", "#CCCCCC"))
        return f"{value:g}{(' ' + unit) if unit else ''}"

    def _redraw(self):
        if not self._selected_pubkey:
            return
        since = (time.time() - self._range_s) if self._range_s else 0
        self._plot_data = {}
        total = 0
        for metric, (curve, _label, _unit, _primary) in self._plot_curves.items():
            data = self.store.samples(self._selected_pubkey, metric, since=since)
            xs = [ts for ts, _v in data]
            ys = [v for _ts, v in data]
            curve.setData(xs, ys)
            self._plot_data[metric] = (xs, ys)
            total += len(xs)
        self.plot.enableAutoRange()

        # sample count in the title + a hint when the range is empty
        entry = self.store.repeater(self._selected_pubkey)
        title = entry[1] if entry else (self._selected_pubkey[:12] + "…")
        self.lbl_graph_title.setText(
            f"{title} — history ({total} sample"
            f"{'s' if total != 1 else ''} in range)")
        if total == 0:
            newest = self.store.newest_sample(
                self._selected_pubkey, self.metric_sel.currentText())
            if newest is None:
                newest = self.store.newest_sample_any(self._selected_pubkey)
            if newest:
                ts, metric = newest[0], newest[1]
                self.lbl_hint.setText(
                    "no samples in the selected range — newest sample: "
                    + time.strftime("%d %b %H:%M", time.localtime(ts))
                    + f" ({metric}) — widen the range to see it")
            else:
                self.lbl_hint.setText(
                    "no data recorded yet — poll this repeater to collect"
                    " history")
            self.lbl_hint.show()
        else:
            self.lbl_hint.hide()

    def _on_mouse_moved(self, pos):
        """Hover: crosshair + the value of the nearest sample of every
        plotted metric. Rendered INSIDE the plot so the window never resizes."""
        data = getattr(self, "_plot_data", None)
        vline = getattr(self, "hover_vline", None)
        text = getattr(self, "hover_text", None)
        if not data or vline is None or text is None:
            return
        if not self.plot.sceneBoundingRect().contains(pos):
            vline.hide(); text.hide()
            return
        mouse = self.plot.getPlotItem().vb.mapSceneToView(pos)
        mx = mouse.x()
        parts = []
        snap_x = None
        snap_dx = None
        for metric, (xs, ys) in data.items():
            if not xs:
                continue
            i = min(range(len(xs)), key=lambda k: abs(xs[k] - mx))
            if snap_x is None or abs(xs[i] - mx) < snap_dx:
                snap_x, snap_dx = xs[i], abs(xs[i] - mx)
            label, unit, _c, _primary = self._plot_curves[metric]
            val = (fmt_duration(ys[i]) if metric == "uptime_s"
                   else f"{ys[i]:g}{(' ' + unit) if unit else ''}")
            when = time.strftime("%d %b %H:%M:%S", time.localtime(xs[i]))
            parts.append(f"<b>{label}:</b> {val} <i>({when})</i>")
        if not parts:
            vline.hide(); text.hide()
            return
        vline.setPos(snap_x)
        text.setPos(mx, mouse.y())
        text.setHtml("<div style='background:rgba(16,21,28,210);"
                     "border:1px solid #30363d;border-radius:6px;padding:4px 6px;'>"
                     + "<br>".join(parts) + "</div>")
        text.show()
        vline.show()

    # ------------------------------------------------------------ map
    def _update_map_markers(self, batt_all=None, temp_all=None,
                            rssi_all=None, st_all=None, te_all=None,
                            noise_all=None):
        """Push positions (from the store, populated by contact syncs) into
        the map as pins — only repeaters MARKED for polling. Repeaters
        without a fix are skipped. Pins use the UI accent blue; markers carry
        a stats dict for the hover card; link lines from the companion are
        colored by the last measured RSSI (green strong / blue good /
        orange weak / grey no data)."""
        if batt_all is None:
            batt_all = self.store.latest_samples_all("battery_v")
            temp_all = self.store.latest_samples_all("temperature_c")
            rssi_all = self.store.latest_samples_all("rssi")
            noise_all = self.store.latest_samples_all("noise_floor")
            st_all = self.store.last_poll_all("status")
            te_all = self.store.last_poll_all("telemetry")
        pos = self.store.positions()
        now = time.time()
        markers = []
        links = []
        clat, clon = self._companion_pos or (None, None)
        for (pubkey, name, _pw, _ls, _le, poll_on,
             _ph, _pm, _vc) in self.store.repeaters():
            if not poll_on:
                continue
            if pubkey not in pos:
                continue
            lat, lon = pos[pubkey]
            poll_ts = max([x[0] for x in (st_all.get(pubkey),
                                          te_all.get(pubkey)) if x] or [None])
            rssi = rssi_all.get(pubkey)
            stats = {
                "rssi": rssi,
                "noise": noise_all.get(pubkey),
                "batt": batt_all.get(pubkey),
                "temp": temp_all.get(pubkey),
                "last": (time.strftime("%H:%M:%S", time.localtime(poll_ts))
                         if poll_ts else None),
                "hops": self._hops_text(pubkey),
            }
            markers.append((pubkey, lat, lon, name or pubkey[:12], stats))
            # link line companion -> repeater, colored by measured RSSI
            if clat is not None:
                if rssi is None:
                    color = theme.TEXT_MUTE
                elif rssi >= -85:
                    color = theme.OK
                elif rssi >= -100:
                    color = theme.ACCENT
                else:
                    color = theme.WARN
                links.append((clat, clon, lat, lon, color))
        self.map_view.set_markers(markers)
        self.map_view.set_links(links)

    def _set_basemap(self, style):
        self.map_view.set_basemap(style)
        for st, b in self.basemap_buttons.items():
            b.setChecked(st == style)
        self.lbl_map_attr.setText(BASEMAPS[style][1])

    def _on_marker_clicked(self, pubkey):
        for i in range(self.table.rowCount()):
            it = self.table.item(i, COL_PK)
            if it and it.data(Qt.UserRole) == pubkey:
                self.table.selectRow(i)
                break
        self.pages.setCurrentIndex(PAGE_OVERVIEW)   # History chart forward
        self.map_view.center_on_marker(pubkey)

    def _center_map_on_selection(self):
        if not self._selected_pubkey:
            return
        pos = self.store.positions().get(self._selected_pubkey)
        if pos:
            self.map_view.center_on_marker(self._selected_pubkey)
        else:
            self.statusBar().showMessage(
                "Selected repeater has no known position", 4000)

    def _append_log(self, line):
        self.log_view.append(time.strftime("[%H:%M:%S] ") + line)

    # ------------------------------------------------------------ clean up
    def _cleanup_list(self):
        """Remove repeaters the companion no longer knows.

        The companion evicts the oldest non-favourite contact when its list
        is full, so some repeaters MeshMon once synced may no longer exist
        device-side (they show "—" hops and dim). This removes them — and
        their history — after a confirmation that lists everything."""
        if not self.radio.mc:
            QMessageBox.information(self, "MeshMonCore", "Not connected.")
            return
        unknown = []
        for (pubkey, name, _pw, _ls, _le, _on, _ph, _pm, _vc) \
                in self.store.repeaters():
            if self.radio.find_contact(pubkey) is None:
                unknown.append((pubkey, name))
        if not unknown:
            QMessageBox.information(
                self, "MeshMonCore",
                "Nothing to clean up — the companion knows every repeater"
                " in the list.")
            return
        # The dialog is size-bounded (list scrolls internally) so cleaning
        # up 100+ repeaters can never overflow the monitor.
        box = QDialog(self)
        box.setWindowTitle("Clean up repeater list")
        box.resize(520, 420)
        v = QVBoxLayout(box)
        lbl = QLabel(
            f"The companion no longer knows {len(unknown)} repeater(s)."
            " Remove them (and their history)? They re-register"
            " automatically if it hears them again.")
        lbl.setWordWrap(True)
        v.addWidget(lbl)
        lst = QListWidget()
        lst.setAlternatingRowColors(True)
        for pk, name in unknown:
            lst.addItem(f"{name or pk[:12] + '…'}  —  {pk[:12]}…")
        lst.setMaximumHeight(240)      # scrolls internally beyond this
        v.addWidget(lst)
        h2 = QHBoxLayout()
        h2.addStretch(1)
        btn_remove = QPushButton(f"Remove {len(unknown)}")
        btn_remove.setProperty("danger", True)
        btn_cancel = QPushButton("Cancel")
        h2.addWidget(btn_remove)
        h2.addWidget(btn_cancel)
        v.addLayout(h2)

        confirmed = {"ok": False}

        def _do_remove():
            confirmed["ok"] = True
            box.accept()

        btn_remove.clicked.connect(_do_remove)
        btn_cancel.clicked.connect(box.reject)
        box.exec()
        if not confirmed["ok"]:
            return
        removed = 0
        for pk, _name in unknown:
            self.store.forget_repeater(pk)
            removed += 1
        if self._selected_pubkey and self.store.repeater(
                self._selected_pubkey) is None:
            self._selected_pubkey = None
        self._companion_pos = None   # (lat, lon) from the handshake, for links
        self._table_filter = ""
        self._dash_metrics = ["battery_v", "temperature_c", "rssi", "snr",
                              "noise_floor", "uptime_s", "util_tx",
                              "util_rx"]
        self._dash_rank = "last_heard"
        self._dash_range_s = 24 * 3600
        self._dash_n = 10
        self._dash_sig = None
        self._dash_sort = (0, Qt.SortOrder.AscendingOrder)   # (column, order)
        self._append_log(f"cleaned up {removed} repeater(s) unknown to the"
                         " companion")
        self._refresh_views()

    # ------------------------------------------------------------- path
    def _edit_path(self):
        """View the route to the selected repeater and set the primary /
        secondary paths.

        Hops are added one per line ("Add hop" appends a new row, then you
        pick the repeater in that row's dropdown). Both paths are remembered
        per repeater; the primary is re-applied on every connect, the
        secondary is pushed onto the companion automatically while poll
        tries 5-6 run (after which the primary is restored)."""
        if not self._selected_pubkey:
            QMessageBox.information(self, "MeshMonCore", "Select a repeater first.")
            return
        from PySide6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
            QComboBox, QLabel, QWidget, QScrollArea, QGroupBox,
        )
        pubkey = self._selected_pubkey
        entry = self.store.repeater(pubkey)
        name = entry[1] if entry else pubkey[:12]
        saved_hex, saved_mode = self.store.get_path(pubkey)
        saved2_hex, saved2_mode = self.store.get_path2(pubkey)

        raw = self.radio.raw_path(pubkey)
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Path — {name}")
        dlg.resize(560, 640)
        v = QVBoxLayout(dlg)

        cur = QLabel()
        if raw:
            path_hex, plen, mode = raw
            if plen == 0:
                cur.setText("Current path: direct (0 hops, mesh-computed)")
            elif plen in (255, -1):
                cur.setText("Current path: unknown (no path learned yet)")
            else:
                cur.setText(f"Current path: {plen} hop(s) — "
                            f"{path_hex[:plen * 2]} (hash mode {mode})")
        else:
            cur.setText("Current path: unknown")
        v.addWidget(cur)

        # alphabetical contact list shared by every hop dropdown
        contacts = [c for c in self.radio.contacts()
                    if c.get("type") == 2
                    and (c.get("public_key") or "").lower() != pubkey.lower()]
        contacts.sort(key=lambda c: (c.get("adv_name") or "").lower())

        def make_combo(preselect_hash=None):
            combo = QComboBox()
            combo.addItem("— select repeater —", None)
            for c in contacts:
                pk = c.get("public_key").lower()
                nm = c.get("adv_name") or pk[:12]
                combo.addItem(f"{nm}  ({pk[:12]}…)", pk)
            if preselect_hash:
                h = preselect_hash.lower()
                idx = next((i for i in range(combo.count())
                            if (combo.itemData(i) or "").startswith(h)), None)
                if idx is None:
                    combo.addItem(f"unknown hop ({h})", "raw:" + h)
                    combo.setCurrentIndex(combo.count() - 1)
                else:
                    combo.setCurrentIndex(idx)
            return combo

        def make_editor(title, prefill_hex, prefill_mode):
            """One path editor: hash-size selector + per-hop rows."""
            grp = QGroupBox(title)
            gl = QVBoxLayout(grp)
            gl.setContentsMargins(8, 6, 8, 6)

            mode_row = QHBoxLayout()
            mode_row.addWidget(QLabel("Hash size per hop:"))
            mode_sel = QComboBox()
            # exactly three options per the hardware: 1, 2 or 3 bytes per hop
            mode_sel.addItems(["1 byte (mode 0)", "2 bytes (mode 1)",
                               "3 bytes (mode 2)"])
            default_mode = (prefill_mode if prefill_hex
                            else self.path_hash.currentIndex())
            mode_sel.setCurrentIndex(min(max(default_mode, 0), 2))
            mode_row.addWidget(mode_sel)
            mode_row.addStretch(1)
            gl.addLayout(mode_row)

            rows = []
            hops_box = QWidget()
            hops_lay = QVBoxLayout(hops_box)
            hops_lay.setContentsMargins(0, 0, 0, 0)
            hops_lay.setSpacing(4)
            gl.addWidget(hops_box)

            def renumber():
                for i, row in enumerate(rows):
                    row["label"].setText(f"{i + 1}.")

            def del_hop(row):
                hops_lay.removeWidget(row["widget"])
                row["widget"].deleteLater()
                rows.remove(row)
                renumber()

            def add_hop(preselect_hash=None):
                widget = QWidget()
                h = QHBoxLayout(widget)
                h.setContentsMargins(0, 0, 0, 0)
                lbl = QLabel(f"{len(rows) + 1}.")
                combo = make_combo(preselect_hash)
                btn_del = QPushButton("✕")
                btn_del.setFixedWidth(32)
                h.addWidget(lbl)
                h.addWidget(combo, 1)
                h.addWidget(btn_del)
                row = {"widget": widget, "combo": combo, "label": lbl}
                btn_del.clicked.connect(lambda _c=False, r=row: del_hop(r))
                hops_lay.addWidget(widget)
                rows.append(row)

            if prefill_hex:
                nbytes = prefill_mode + 1
                for i in range(0, len(prefill_hex), nbytes * 2):
                    add_hop(prefill_hex[i:i + nbytes * 2])

            btn_add_hop = QPushButton("Add hop")
            btn_add_hop.clicked.connect(lambda _c=False: add_hop())
            gl.addWidget(btn_add_hop)

            def build_hex():
                # mode index == bytes-1: "1 byte (mode 0)" -> 1 byte per hop.
                # Reading the leading DIGIT of the label instead made the
                # repeater count hops 3x too coarsely.
                nbytes = mode_sel.currentIndex() + 1
                out = ""
                for row in rows:
                    data = row["combo"].currentData()
                    if not data:
                        continue
                    if data.startswith("raw:"):
                        out += data[4:]
                    else:
                        out += data[:nbytes * 2]
                return out

            return {"group": grp, "mode": mode_sel, "build": build_hex}

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setContentsMargins(0, 0, 0, 0)
        ed_primary = make_editor(
            "Primary path (tries 1-4)", saved_hex, saved_mode)
        iv.addWidget(ed_primary["group"])
        ed_secondary = make_editor(
            "Secondary path (used automatically on tries 5-6 when the"
            " primary fails)", saved2_hex, saved2_mode)
        iv.addWidget(ed_secondary["group"])
        iv.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)

        def apply_primary():
            path_hex = ed_primary["build"]()
            mode = ed_primary["mode"].currentIndex()
            if not path_hex:
                self.radio.clear_path_async(pubkey)
            else:
                self.radio.set_path(pubkey, path_hex, mode)
            dlg.accept()

        def apply_secondary():
            path_hex = ed_secondary["build"]()
            mode = ed_secondary["mode"].currentIndex()
            self.radio.set_path2(pubkey, path_hex, mode)
            dlg.accept()

        btn_apply = QPushButton("Apply primary")
        btn_apply.clicked.connect(apply_primary)
        btn_apply2 = QPushButton("Apply secondary")
        btn_apply2.clicked.connect(apply_secondary)
        btn_clear2 = QPushButton("Clear secondary")
        btn_clear2.setToolTip("Remove the secondary path — fallback tries"
                              " 5-6 then simply retry the primary path.")
        btn_clear2.clicked.connect(
            lambda: (self.radio.clear_path2_async(pubkey), dlg.accept()))
        btn_direct = QPushButton("Direct (0 hops)")
        btn_direct.setToolTip(
            "Set an explicit 0-hop DIRECT path — only sane for a 1-hop"
            " neighbour you can reach directly.")
        btn_direct.clicked.connect(
            lambda: self.radio.set_direct_path_async(pubkey))
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(dlg.reject)

        h2 = QHBoxLayout()
        h2.addWidget(btn_apply)
        h2.addWidget(btn_apply2)
        h2.addWidget(btn_clear2)
        h2.addWidget(btn_direct)
        h2.addWidget(btn_cancel)
        h2.addStretch(1)
        v.addLayout(h2)
        dlg.exec()
        self._refresh_views()

    # -------------------------------------------------- repeater context menu
    def _repeater_menu(self, pos):
        from PySide6.QtWidgets import QMenu
        menu = QMenu(self)
        act_poll = menu.addAction("Poll (6 tries)")
        act_path = menu.addAction("Path…")
        menu.addSeparator()
        act_del = menu.addAction("Remove from list")
        act = menu.exec(self.table.viewport().mapToGlobal(pos))
        if act == act_poll and self._selected_pubkey:
            self.radio.poll_now(self._selected_pubkey, tries=6)
        elif act == act_path and self._selected_pubkey:
            self._edit_path()
        elif act == act_del and self._selected_pubkey:
            self.store.forget_repeater(self._selected_pubkey)
            self._selected_pubkey = None
            self._refresh_views()

    def showEvent(self, ev):
        # first real show: the window now knows which screen it is on
        # (primary at __init__ time may not be the one the WM picked)
        if not getattr(self, "_shown_once", False):
            self._shown_once = True
            self._clamp_to_screen(self._mouse_screen())
        super().showEvent(ev)

    def closeEvent(self, ev):
        self._save_settings()
        self.radio.stop()
        super().closeEvent(ev)
