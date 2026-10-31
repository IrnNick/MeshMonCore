"""OpenStreetMap slippy-map widget for MeshMonCore — QtNetwork + disk cache, no
extra dependencies beyond PySide6 (QtNetwork ships in the essentials bundle).

Coordinate system: scene units are WORLD PIXELS at the current integer zoom z
(standard slippy-map scheme). One OSM tile = 256 px.

Marker clustering (same pattern as Leaflet.markercluster on map.meshcore.io):
markers are grouped on an ~80 px world grid at the current zoom. A cell with
2+ repeaters renders as a numbered bubble; clicking it zooms in one level
centered on the group. A cell with a single repeater renders the full
composite marker (grey circle + white radio tower + name plate).

Zooming is SMOOTH: the view zoom is continuous (_eff, animated) — during
the gesture the existing tiles and markers scale with the view and nothing
refetches; when the gesture settles, the tile zoom commits to the nearest
integer (one rebuild, one fetch batch). Wheel notches accumulate, so a fast
multi-notch spin zooms several levels in one animation.

Tiles are cached on disk (cache_dir/z/x_y.png) and fetched from
tile.openstreetmap.org with a proper User-Agent (per OSM tile policy).
"""
import math
import os
import time

from PySide6.QtCore import (QEasingCurve, Qt, QEvent, QPointF, QPoint,
                            QRectF, QRect, QTimer, QUrl, QVariantAnimation,
                            Signal)
from PySide6.QtGui import QColor, QImage, QFont, QPainter, QPen, QPixmap, QTransform
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import (QGraphicsPixmapItem, QGraphicsScene,
                               QGraphicsView, QGraphicsLineItem, QToolTip)
from PySide6.QtCore import QLineF

from . import theme, __version__

TILE = 256
UA = f"MeshMonCore/{__version__} (MeshCore repeater monitor; one-off user requests only)"

# basemap styles: name -> (url template, attribution)
BASEMAPS = {
    "dark": ("https://server.arcgisonline.com/ArcGIS/rest/services/"
             "Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
             "Esri, HERE, Garmin, © OpenStreetMap contributors"),
    "satellite": ("https://server.arcgisonline.com/ArcGIS/rest/services/"
                  "World_Imagery/MapServer/tile/{z}/{y}/{x}",
                  "Imagery © Esri, Maxar, Earthstar Geographics"),
    "osm": ("https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "© OpenStreetMap contributors"),
    "3d": ("https://server.arcgisonline.com/ArcGIS/rest/services/"
           "World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
           "Esri, HERE, Garmin, © OpenStreetMap contributors — topographic:"
           " relief, roads, places"),
}
# deepest native tile zoom per style — beyond it, tiles are overzoomed
# (fetched at the style's max and scaled up by 2^(z - max)). 3d = Esri
# World_Topo_Map serves native z19 in populated areas (probed 2026-10).
STYLE_MAX_Z = {"dark": 16, "satellite": 19, "osm": 19, "3d": 19}
# disk-cache sub-directory per style. Bump the suffix whenever a style's
# tile URL changes — otherwise the old cached tiles keep being served from
# disk (3d moved from World_Shaded_Relief to World_Topo_Map in 2026-10 and
# every cached tile was a shaded-relief image).
STYLE_CACHE_DIR = {"dark": "dark", "satellite": "satellite",
                   "osm": "osm", "3d": "3d_topo"}
MIN_Z, MAX_Z = 3, 19
CLUSTER_CELL = 80      # world-px grid cell for clustering
PAN_DEAD_ZONE = 5      # px a grab may jitter without panning or killing a click


def _ll_to_px(lat, lon, z):
    """lat/lon -> world pixel coords at zoom z (Web Mercator)."""
    n = float(2 ** z * TILE)
    x = (lon + 180.0) / 360.0 * n
    la = max(-85.05112878, min(85.05112878, lat))
    la = math.radians(la)
    y = (1.0 - math.log(math.tan(la) + 1.0 / math.cos(la)) / math.pi) / 2.0 * n
    return x, y


def _px_to_lat(x, y, z):
    n = float(2 ** z * TILE)
    lon = x / n * 360.0 - 180.0
    yy = max(-1.0, min(1.0, y / n))
    la = math.atan(math.sinh(math.pi * (1.0 - 2.0 * yy)))
    return math.degrees(la), lon


def _measure_text(name, f):
    """TRUE ink extent of `name` when painted with font `f`: render onto a
    scratch image and scan the non-transparent pixels.

    Font metrics (QFontMetrics.horizontalAdvance and even
    QPainter.boundingRect) under-report fallback glyphs such as color-emoji
    flags — a repeater named "🇧🇬 Sofia R1" measured ~72 px but painted
    wider, clipping the flag in half. Measuring the actual painted ink is the
    only reliable number."""
    W, H = 1200, 80
    img = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRectF(4, 8, W - 8, H - 16),
               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
               name)
    p.end()
    buf = bytes(img.constBits())          # BGRA, 4 bytes/px, W*4 already 4-aligned
    alphas = buf[3::4]
    ink = [i for i, a in enumerate(alphas) if a]
    if not ink:
        return 0.0, 0.0
    min_x = min(i % W for i in ink)
    max_x = max(i % W for i in ink)
    min_y = min(i // W for i in ink)
    max_y = max(i // W for i in ink)
    return float(max_x - min_x + 1), float(max_y - min_y + 1)


def _marker_pixmap(name):
    """Composite marker: small grey circle with a white radio tower on top,
    the repeater name below in white on a grey rounded plate. Rendered at 2x
    and halved via devicePixelRatio so text and glyph stay crisp. The anchor
    point (circle center) is the repeater's position.

    Two geometry rules must both hold (an earlier version violated the second
    and chopped the plate's bottom off):
      - the pixmap is tall enough for the FULL plate: H >= plate_top + th + 4;
      - the plate is tall enough for the text: th from the ink-scan height
        (metrics APIs under-report color-emoji glyphs) + 8 px."""
    from PySide6.QtCore import QRectF
    f = QFont("DejaVu Sans", 6)
    f.setWeight(QFont.Weight.DemiBold)
    ink_w, ink_h = _measure_text(name, f)
    tw = int(ink_w) + 8        # 4 px per side — the flag emoji is included in
    th = max(12, int(ink_h) + 5)  # the ink scan, so tight padding is safe
    circ_d = 14
    W = max(circ_d + 6, int(tw))
    plate_top = circ_d + 2
    H = int(plate_top + th + 4)
    dpr = 2.0
    pm = QPixmap(int(W * dpr), int(H * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setFont(f)
    cx, cy = W / 2.0, circ_d / 2.0 + 0.5

    # name plate (navy card look, rounded, thin border), light text
    plate = QRectF(cx - tw / 2, plate_top, tw, th)
    p.setPen(QPen(QColor(theme.BORDER), 1))
    p.setBrush(QColor(17, 26, 40, 235))                # CARD navy, near-opaque
    p.drawRoundedRect(plate, 3, 3)
    p.setPen(QColor(theme.TEXT))
    p.drawText(plate, Qt.AlignmentFlag.AlignCenter, name)

    # status-colored circle + white radio tower glyph
    p.setPen(QPen(QColor(theme.BG), 1.2))
    p.setBrush(QColor(theme.ACCENT))
    p.drawEllipse(QPointF(cx, cy), circ_d / 2 - 0.6, circ_d / 2 - 0.6)
    mast_top = cy - circ_d * 0.18
    mast_bot = cy + circ_d * 0.30
    p.setPen(QPen(QColor("#ffffff"), 1.3))
    p.drawLine(QPointF(cx, mast_bot), QPointF(cx, mast_top))
    p.setBrush(Qt.BrushStyle.NoBrush)
    for r in (circ_d * 0.20, circ_d * 0.36):
        p.drawArc(QRectF(cx - r, mast_top - r, 2 * r, 2 * r), 30 * 16, 120 * 16)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#ffffff"))
    p.drawEllipse(QPointF(cx, mast_top), 1.2, 1.2)
    p.end()
    return pm, W / 2.0, circ_d / 2.0 + 0.5    # pixmap, anchor x, anchor y


def _cluster_pixmap(count):
    """Numbered cluster bubble: grey circle, white bold count, light ring."""
    d = 34 if count < 100 else 42
    dpr = 2.0
    pm = QPixmap(int(d * dpr), int(d * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = d / 2.0
    p.setPen(QPen(QColor(theme.ACCENT_D), 2))
    p.setBrush(QColor(47, 111, 224, 235))
    p.drawEllipse(QPointF(c, c), d / 2 - 2, d / 2 - 2)
    f = QFont("DejaVu Sans", 9 if count < 100 else 8)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    # explicit logical rect: pm.rect() is in DEVICE pixels while the painter
    # works in logical units under the 2x devicePixelRatio — centering in
    # pm.rect() threw the number past the bubble's bottom-right corner
    p.drawText(QRectF(0, 0, d, d), Qt.AlignmentFlag.AlignCenter, str(count))
    p.end()
    return pm, d / 2.0, d / 2.0


def _marker_tooltip(name, stats):
    """Rich hover card for a pin: name + live readings, styled like a card."""
    if not isinstance(stats, dict) or not stats:
        return name or ""
    def row(k, v):
        return (f"<tr><td style='color:#8a97a8;padding-right:14px;'>{k}</td>"
                f"<td style='color:#e6edf6;font-weight:bold;'>{v}</td></tr>")
    rssi, batt, temp = stats.get("rssi"), stats.get("batt"), stats.get("temp")
    noise = stats.get("noise")
    rows = ""
    rows += row("RSSI", f"{rssi:.0f} dBm" if rssi is not None else "—")
    rows += row("Noise floor", f"{noise:.0f} dBm" if noise is not None else "—")
    rows += row("Battery", f"{batt:.2f} V" if batt is not None else "—")
    rows += row("Temp", f"{temp:.1f} °C" if temp is not None else "—")
    rows += row("Last seen", stats.get("last") or "—")
    rows += row("Hops", stats.get("hops") or "—")
    return ("<div style='background:#111a28;border:1px solid #1e2a3d;"
            "border-radius:8px;padding:8px 10px;'>"
            "<div style='color:#e6edf6;font-weight:bold;"
            "margin-bottom:4px;font-size:11pt;'>"
            f"{name}</div><table cellspacing='0'>{rows}</table></div>")


class PinItem(QGraphicsPixmapItem):
    """Marker pixmap carrying its hover-card HTML. The card itself is shown
    by SlippyMap's mouse tracking (manual hit-test) — Qt's per-item hover
    events did not fire reliably under the pan/zoom mouse handling."""

    def __init__(self, tooltip_html, pixmap=None):
        super().__init__(pixmap)
        self._tip = tooltip_html

    def tooltip(self):
        return self._tip


class SlippyMap(QGraphicsView):
    """Pan/zoom OSM map with repeater markers and cluster bubbles."""
    marker_clicked = Signal(str)      # pubkey hex
    tiles_fetched = Signal(int)       # tiles pulled from the network this batch

    def __init__(self, cache_dir, parent=None):
        super().__init__(parent)
        self._z = 10             # TILE zoom: scene coords + the loaded tiles
        self._eff = 10.0         # CONTINUOUS zoom: view scale = 2^(_eff - _z)
        self._zoom_target = 10.0
        self._center_ll = (0.0, 0.0)
        self._zoom_timer = None   # manual eased-zoom driver (QTimer, 16 ms)
        self._scene = QGraphicsScene(self)
        self._scene.setBackgroundBrush(QColor(theme.BG))
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setMouseTracking(True)   # hover cards: track without a button
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._tiles = {}         # (z, x, y) -> QGraphicsPixmapItem
        self._pending = set()    # (z, x, y) currently downloading
        self._markers = []       # [(pubkey, lat, lon, name[, stats])]
        self._map_items = []     # markers + cluster bubbles currently drawn
        self._links = []         # [(lat1, lon1, lat2, lon2, color)]
        self._link_items = []    # link QGraphicsLineItems currently drawn
        self._basemap = "dark"   # key into BASEMAPS
        self._cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.tiles_enabled = True            # tests can disable network fetches

        self._nam = QNetworkAccessManager(self)
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setSingleShot(True)
        self._fetch_timer.setInterval(120)
        self._fetch_timer.timeout.connect(self._fetch_visible)

        self._pan_start = None
        self._pan_base_h = 0
        self._pan_base_v = 0
        self._pan_moved = False
        self._apply_scene_rect()
        self._apply_view_transform()

    # ---- coordinates ------------------------------------------------------
    def _ll_to_scene(self, lat, lon):
        x, y = _ll_to_px(lat, lon, self._z)
        return QPointF(x, y)

    def _effective_zoom(self):
        return self._eff

    def center_latlon(self):
        return self._center_ll

    def set_center(self, lat, lon):
        self._center_ll = (lat, lon)
        self._apply_scene_rect()
        self._apply_view_transform()
        self.centerOn(*_ll_to_px(lat, lon, self._z))
        self._schedule_fetch()

    def set_zoom(self, z):
        """Restore a saved integer tile zoom (settings load) without the
        eased-animation path."""
        self._z = int(z)
        self._eff = float(self._z)
        self._zoom_target = float(self._z)

    def set_zoom_step(self, d):
        """One zoom notch: notches ACCUMULATE (a fast multi-notch spin zooms
        several levels in one animation). The tiles rescale during the
        animation and refetch once, when it settles."""
        self._zoom_target = max(MIN_Z, min(MAX_Z, round(self._zoom_target) + d))
        self._animate_zoom_to(self._zoom_target)

    def _animate_zoom_to(self, target_e):
        target_e = max(MIN_Z, min(MAX_Z, target_e))
        self._zoom_target = target_e
        if abs(target_e - self._eff) < 1e-9:
            return
        if self._zoom_timer is not None:
            self._zoom_timer.stop()
            self._zoom_timer = None
        # Manual eased-zoom driver. QVariantAnimation was unreliable here:
        # in the full app it ran its duration and emitted finished WITHOUT a
        # single valueChanged (the view never moved), while a QTimer ticks
        # fine — so interpolate _eff ourselves on a 16 ms timer.
        start = self._eff
        state = {"t0": time.monotonic()}
        dur = 0.22   # seconds (time.monotonic() units)

        def tick():
            t = min(1.0, (time.monotonic() - state["t0"]) / dur)
            self._apply_eff(start + (target_e - start)
                            * (1.0 - (1.0 - t) ** 3))   # easeOutCubic
            if t >= 1.0:
                self._zoom_timer.stop()
                self._zoom_timer = None
                self._zoom_finished()

        self._zoom_timer = QTimer(self)
        self._zoom_timer.setInterval(16)
        self._zoom_timer.timeout.connect(tick)
        self._zoom_timer.start()

    def _zoom_finished(self):
        self._zoom_anim = None
        self._zoom_target = round(self._eff)
        self._settle()

    def _apply_eff(self, e):
        """Continuous zoom: rescale the view only — tiles and markers ride
        along scaled (no refetch, no rebuild) until the gesture settles."""
        self._eff = float(e)
        self._apply_view_transform()
        self.centerOn(*_ll_to_px(self._center_ll[0], self._center_ll[1],
                                 self._z))

    def _settle(self):
        """Commit the tile zoom to the nearest integer of the effective zoom
        (one rebuild + one fetch batch per gesture, not per notch)."""
        z_new = max(MIN_Z, min(MAX_Z, round(self._eff)))
        if z_new != self._z:
            self._z = z_new
            self._apply_scene_rect()
            self._clear_tiles()
            self._rebuild_map_items()
            self._rebuild_links()
        self._apply_view_transform()
        self.centerOn(*_ll_to_px(self._center_ll[0], self._center_ll[1],
                                 self._z))
        self._schedule_fetch()

    def _apply_view_transform(self):
        k = 2.0 ** (self._eff - self._z)
        self.setTransform(QTransform.fromScale(k, k))

    def _apply_scene_rect(self):
        n = 2 ** self._z * TILE
        self._scene.setSceneRect(0, 0, n, n)

    # ---- markers / clusters -------------------------------------------------
    def set_markers(self, markers):
        """markers: [(pubkey, lat, lon, name[, stats])] — replaces all pins.
        The optional stats dict feeds the hover card."""
        if markers == self._markers:
            return
        self._markers = list(markers)
        self._rebuild_map_items()

    def _marker_scale(self):
        """Marker size relative to the tile zoom: full size from z=11 up,
        shrinking to half when zoomed out — pins otherwise dwarf the mapped
        area at low zooms."""
        return max(0.5, min(1.0, 0.5 + 0.125 * (self._z - 5)))

    def _rebuild_map_items(self):
        for it in self._map_items:
            if it.scene() is self._scene:
                self._scene.removeItem(it)
        self._map_items = []

        # grid-cluster the markers in world-px space at the current zoom
        groups = {}
        for pk, lat, lon, name, *extra in self._markers:
            x, y = _ll_to_px(lat, lon, self._z)
            gkey = (int(x // CLUSTER_CELL), int(y // CLUSTER_CELL))
            stats = extra[0] if extra else None
            groups.setdefault(gkey, []).append((pk, lat, lon, name, x, y,
                                                stats))

        for members in groups.values():
            if len(members) == 1:
                (pk, lat, lon, name, x, y, *mextra) = members[0]
                mstats = mextra[0] if mextra else None
                pm, ax, ay = _marker_pixmap(name or pk[:12] + "\u2026")
                it = PinItem(_marker_tooltip(
                    name or pk[:12] + "\u2026", mstats), pm)
                # Shrink pins when zoomed out so they don't dwarf the mapped
                # area. Explicit translate→scale→translate about the anchor:
                # scale about the item origin instead would also scale the
                # offset and drag pins toward the world origin, and
                # setTransformOriginPoint is not honored for this mapping.
                sc = self._marker_scale()
                t = QTransform()
                t.translate(x, y)
                t.scale(sc, sc)
                t.translate(-ax, -ay)
                it.setTransform(t)
                it.setZValue(10)
                it.setData(0, pk)
            else:
                cx = sum(m[4] for m in members) / len(members)
                cy = sum(m[5] for m in members) / len(members)
                pm, ax, ay = _cluster_pixmap(len(members))
                it = PinItem(f"<b>{len(members)}</b> repeaters — zoom in to"
                             " split", pm)
                it.setOffset(cx - ax, cy - ay)
                it.setZValue(10)
                it.setData(0, "cluster")
            self._scene.addItem(it)
            self._map_items.append(it)
        self._schedule_fetch()

    def center_on_cluster(self, cx, cy):
        """Zoom in one level, centered on a cluster bubble."""
        lat, lon = _px_to_lat(cx, cy, self._z)
        self._center_ll = (lat, lon)
        self._zoom_target = round(self._zoom_target) + 1
        self._animate_zoom_to(self._zoom_target)

    def center_on_marker(self, pubkey):
        """Center on a repeater pin, zooming in if the view is far out."""
        for pk, lat, lon, _name, *_ in self._markers:
            if pk == pubkey:
                self._center_ll = (lat, lon)
                if self._eff < 12:
                    self._zoom_target = 12
                    self._animate_zoom_to(12)
                else:
                    self.centerOn(*_ll_to_px(lat, lon, self._z))
                self._schedule_fetch()
                return

    # ---- tiles -------------------------------------------------------------
    def _clear_tiles(self):
        for it in self._tiles.values():
            if it.scene() is self._scene:
                self._scene.removeItem(it)
        self._tiles = {}
        self._pending = set()

    def _tile_url(self, z, x, y):
        return BASEMAPS[self._basemap][0].format(z=z, x=x, y=y)

    def _tile_cache_path(self, z, x, y):
        # per-style subdirectory: the same z/x/y is a different image per style
        return os.path.join(self._cache_dir, STYLE_CACHE_DIR[self._basemap],
                            str(z), f"{x}_{y}.png")

    def set_basemap(self, style):
        """Switch the basemap (keys of BASEMAPS); clears and refetches."""
        if style not in BASEMAPS or style == self._basemap:
            return
        self._basemap = style
        self._clear_tiles()
        self._schedule_fetch()

    def basemap(self):
        return self._basemap

    def set_links(self, links):
        """Straight link lines under the markers:
        [(lat1, lon1, lat2, lon2, color)] — rebuilt on zoom commits."""
        if links == self._links:
            return
        self._links = list(links)
        self._rebuild_links()

    def _rebuild_links(self):
        for it in self._link_items:
            if it.scene() is self._scene:
                self._scene.removeItem(it)
        self._link_items = []
        for lat1, lon1, lat2, lon2, color in self._links:
            p1 = QPointF(*_ll_to_px(lat1, lon1, self._z))
            p2 = QPointF(*_ll_to_px(lat2, lon2, self._z))
            li = QGraphicsLineItem(QLineF(p1, p2))
            pen = QPen(QColor(color), 2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            li.setPen(pen)
            li.setOpacity(0.75)
            li.setZValue(4)          # under markers (10), over tiles (-1)
            self._scene.addItem(li)
            self._link_items.append(li)

    def _schedule_fetch(self):
        self._fetch_timer.start()

    def _fetch_visible(self):
        if not self.tiles_enabled:
            return
        vr = self.mapToScene(self.viewport().rect()).boundingRect()
        # overzoom: styles with a shallower native max zoom serve tiles from
        # their deepest level, scaled up by 2^(z - max)
        maxz = STYLE_MAX_Z.get(self._basemap, MAX_Z)
        fz = min(self._z, maxz)
        k = 2.0 ** (self._z - fz)          # world-px per fetch-tile px
        n_tiles = 2 ** fz
        x0 = max(0, int(vr.left() // (TILE * k)))
        x1 = min(n_tiles - 1, int(vr.right() // (TILE * k)))
        y0 = max(0, int(vr.top() // (TILE * k)))
        y1 = min(n_tiles - 1, int(vr.bottom() // (TILE * k)))
        fetched = 0
        for tx in range(x0, x1 + 1):
            for ty in range(y0, y1 + 1):
                key = (fz, tx, ty)
                if key in self._tiles or key in self._pending:
                    continue
                cp = self._tile_cache_path(fz, tx, ty)
                if os.path.exists(cp):
                    pm = QPixmap(cp)
                    if not pm.isNull():
                        self._insert_tile(fz, tx, ty, pm, k)
                        continue
                self._request_tile(fz, tx, ty, k)
                fetched += 1
        if fetched:
            self.tiles_fetched.emit(fetched)

    def _insert_tile(self, z, x, y, pm, k=1.0):
        it = QGraphicsPixmapItem(pm)
        it.setPos(x * TILE * k, y * TILE * k)
        # stretch each tile slightly so neighbours overlap by ~1 px: integer
        # scene coords map to fractional device coords and otherwise leave a
        # 1-px seam grid between tiles (the stretch scales with overzoom k)
        it.setTransform(QTransform.fromScale(k * 1.004, k * 1.004))
        # at native scale render fast/nearest — smooth filtering of the tiny
        # seam stretch blurred place-name labels; keep smooth only for real
        # upscaling (overzoom), where it beats blocky pixels
        it.setTransformationMode(
            Qt.TransformationMode.SmoothTransformation if k > 1.001
            else Qt.TransformationMode.FastTransformation)
        it.setZValue(-1)
        self._scene.addItem(it)
        self._tiles[(z, x, y)] = it

    def _request_tile(self, z, x, y, k=1.0):
        key = (z, x, y)
        self._pending.add(key)
        req = QNetworkRequest(QUrl(self._tile_url(z, x, y)))
        req.setRawHeader(b"User-Agent", UA.encode())
        req.setTransferTimeout(15000)
        reply = self._nam.get(req)

        def done():
            data = reply.readAll()
            reply.deleteLater()
            self._pending.discard(key)
            if reply.error() == QNetworkReply.NetworkError.NoError and data:
                cp = self._tile_cache_path(z, x, y)
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                with open(cp, "wb") as f:
                    f.write(bytes(data))
                if z == self._z or (z == min(self._z, STYLE_MAX_Z.get(
                        self._basemap, MAX_Z))):   # zoom may have moved on
                    pm = QPixmap()
                    pm.loadFromData(data)
                    if not pm.isNull():
                        self._insert_tile(z, x, y, pm, k)

        reply.finished.connect(done)

    # ---- events ------------------------------------------------------------
    def showEvent(self, e):
        super().showEvent(e)
        self._apply_scene_rect()
        self._schedule_fetch()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._schedule_fetch()

    def viewportEvent(self, e):
        # Swallow Qt's own QEvent::ToolTip help event. QApplication arms a
        # toolTipWakeUp timer on every no-button mouse move, and ~700 ms
        # after the cursor stops over the ACTIVE window it dispatches a
        # ToolTip help event; QGraphicsView forwards it to the scene, where
        # QGraphicsScene::helpEvent() finds no QGraphicsItem::toolTip() on
        # the pins (the card is drawn manually) and calls
        # QToolTip.hideText() — killing the card ~1 s after the cursor came
        # to rest. Unfocused windows skip that dispatch, which is why the
        # card only died while the app was active. The card's full lifetime
        # is managed in _hover_card/leaveEvent, so the help event has
        # nothing to do here.
        if e.type() == QEvent.Type.ToolTip:
            return True
        return super().viewportEvent(e)

    def _hover_card(self, e):
        """Show the pin's hover card when the cursor is over it; hide when
        it leaves. QToolTip de-duplicates identical show requests, so this is
        cheap to call on every move."""
        sp = e.globalPosition().toPoint()
        scene_pos = self.mapToScene(e.position().toPoint())
        hit = None
        # QGraphicsPixmapItem.contains() is unreliable for DPR-scaled,
        # transformed pins (it missed the pin's own visible center) — test
        # the item's scene bounding rect explicitly instead
        for it in self._map_items:
            if it.sceneBoundingRect().contains(scene_pos):
                hit = it
                break
        if hit is not None:
            # msecDisplayTime=INT_MAX: the card must stay for as long as the
            # cursor hovers, including while it rests on a pin (a normal
            # tooltip expires after a text-length-based delay). viewportEvent
            # keeps Qt's ToolTip help event from hiding it; it is hidden
            # explicitly on leave, pan start and move-off-pin below.
            QToolTip.showText(sp + QPoint(14, 10), hit.tooltip(), self,
                              QRect(), 0x7FFFFFFF)
        elif QToolTip.isVisible():
            QToolTip.hideText()

    def leaveEvent(self, e):
        if QToolTip.isVisible():
            QToolTip.hideText()
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        if QToolTip.isVisible():
            QToolTip.hideText()
        if e.button() == Qt.MouseButton.LeftButton:
            self._pan_start = e.position()
            self._pan_base_h = self.horizontalScrollBar().value()
            self._pan_base_v = self.verticalScrollBar().value()
            self._pan_moved = False
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        # hover card: hit-test pins under the cursor and show/hide the card —
        # manual, so it works regardless of Qt's per-item hover delivery
        if self._pan_start is None:
            self._hover_card(e)
        if self._pan_start is not None and (e.buttons() & Qt.MouseButton.LeftButton):
            d = e.position() - self._pan_start
            if not self._pan_moved:
                # dead zone: a grab with click-sized jitter must not pan and
                # must keep counting as a click on release
                if abs(d.x()) <= PAN_DEAD_ZONE and abs(d.y()) <= PAN_DEAD_ZONE:
                    e.accept()
                    return
                self._pan_moved = True
            # scroll relative to the press-point bases — drift-free rounding,
            # and 1:1 in device px (QGraphicsView scrollbars are post-transform)
            h = self.horizontalScrollBar()
            v = self.verticalScrollBar()
            h.setValue(self._pan_base_h - int(round(d.x())))
            v.setValue(self._pan_base_v - int(round(d.y())))
            c = self.mapToScene(self.viewport().rect().center())
            self._center_ll = _px_to_lat(c.x(), c.y(), self._z)
            self._schedule_fetch()
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if (e.button() == Qt.MouseButton.LeftButton
                and self._pan_start is not None and not self._pan_moved):
            p = self.mapToScene(e.position().toPoint())
            # same bounding-rect hit-test as the hover card: the scene's
            # contains() misses DPR-scaled transformed pins
            for it in self._map_items:
                if not it.sceneBoundingRect().contains(p):
                    continue
                pk = it.data(0)
                if pk == "cluster":
                    br = it.sceneBoundingRect().center()
                    lat, lon = _px_to_lat(br.x(), br.y(), self._z)
                    self._center_ll = (lat, lon)
                    self._animate_zoom_to(self._effective_zoom() + 1)
                elif pk is not None:
                    self.marker_clicked.emit(pk)
                break
            self._pan_start = None
            e.accept()
            return
        self._pan_start = None
        super().mouseReleaseEvent(e)

    def wheelEvent(self, e):
        dy = e.angleDelta().y()
        if dy:
            self._animate_zoom_to(self._effective_zoom() + (1 if dy > 0 else -1))
        e.accept()
