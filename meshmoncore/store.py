"""SQLite history store for MeshMon.

Samples are stored long-form: one row per (timestamp, repeater, metric, value).
Metric names:
    battery_v      repeater battery voltage (V)          [status + telemetry]
    temperature_c  MCU / external sensor temperature (C) [telemetry]
    rssi, snr      last packet radio stats               [status]
    uptime_s, airtime_s, rx_airtime_s                    [status]
    sent, recv, sent_flood, sent_direct, recv_flood, recv_direct, dups, errors
    lpp_<ch>_<type>  extra Cayenne-LPP sensor channels reported by the repeater
"""
import sqlite3
import threading
import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS repeaters (
    pubkey     TEXT PRIMARY KEY,      -- full 32-byte pubkey, hex
    name       TEXT,
    guest_pw   TEXT DEFAULT '',
    last_seen  REAL,
    last_error TEXT,
    poll_enabled INTEGER DEFAULT 0,
    voltage_correct INTEGER DEFAULT 1,
    lat          REAL,
    lon          REAL,
    path_hex   TEXT DEFAULT '',
    path_mode  INTEGER DEFAULT 0,
    path2_hex  TEXT DEFAULT '',    -- secondary path: used automatically on
    path2_mode INTEGER DEFAULT 0   -- poll tries 5-6 when the primary fails
);
CREATE TABLE IF NOT EXISTS samples (
    ts     REAL NOT NULL,
    pubkey TEXT NOT NULL,
    metric TEXT NOT NULL,
    value  REAL,
    PRIMARY KEY (ts, pubkey, metric)
);
CREATE TABLE IF NOT EXISTS poll_log (
    ts     REAL NOT NULL,
    pubkey TEXT,
    kind   TEXT,
    ok     INTEGER,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples ON samples (pubkey, metric, ts);
CREATE TABLE IF NOT EXISTS alert_state (
    pubkey     TEXT,
    kind       TEXT,
    state      TEXT,
    last_alert REAL,
    PRIMARY KEY (pubkey, kind)
);
"""


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self):
        """Add columns older databases are missing (safe to re-run)."""
        cols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(repeaters)")}
        for col, ddl in (("poll_enabled", "INTEGER DEFAULT 0"),
                         ("voltage_correct", "INTEGER DEFAULT 1"),
                         ("lat", "REAL"),
                         ("lon", "REAL"),
                         ("path_hex", "TEXT DEFAULT ''"),
                         ("path_mode", "INTEGER DEFAULT 0"),
                         ("path2_hex", "TEXT DEFAULT ''"),
                         ("path2_mode", "INTEGER DEFAULT 0"),
                         ("util_tx", "REAL"),
                         ("util_rx", "REAL")):
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE repeaters ADD COLUMN {col} {ddl}")
        # alert_state: one row per (repeater, alert kind) since voltage
        # alerts joined temperature alerts
        acols = {r[1] for r in self._conn.execute(
            "PRAGMA table_info(alert_state)")}
        if acols and "kind" not in acols:
            self._conn.execute("ALTER TABLE alert_state RENAME TO alert_state_old")
            self._conn.execute(
                "CREATE TABLE alert_state (pubkey TEXT, kind TEXT,"
                " state TEXT, last_alert REAL, PRIMARY KEY (pubkey, kind))")
            self._conn.execute(
                "INSERT OR REPLACE INTO alert_state"
                " (pubkey, kind, state, last_alert)"
                " SELECT pubkey, 'temp', state, last_alert FROM alert_state_old")
            self._conn.execute("DROP TABLE alert_state_old")
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---- repeaters -------------------------------------------------------
    def upsert_repeater(self, pubkey, name=None, guest_pw=None, last_seen=None,
                        last_error=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT name, guest_pw FROM repeaters WHERE pubkey=?",
                (pubkey,)).fetchone()
            name = name if name is not None else (row[0] if row else pubkey[:12])
            pw = guest_pw if guest_pw is not None else (row[1] if row else "")
            ls = last_seen if last_seen is not None else time.time()
            # poll_enabled is set EXPLICITLY here: older databases carry the
            # column with DEFAULT 1 (an early migration default, since fixed
            # in SCHEMA but never applied to an existing table), so omitting
            # it auto-marked every newly registered contact
            self._conn.execute(
                "INSERT INTO repeaters (pubkey, name, guest_pw, last_seen,"
                " last_error, poll_enabled)"
                " VALUES (?,?,?,?,?,0) ON CONFLICT(pubkey) DO UPDATE SET"
                " name=excluded.name, last_seen=excluded.last_seen,"
                " last_error=excluded.last_error",
                (pubkey, name, pw, ls, last_error))
            self._conn.commit()

    def set_guest_pw(self, pubkey, pw):
        self.upsert_repeater(pubkey, guest_pw=pw)

    def repeaters(self):
        """Returns (pubkey, name, guest_pw, last_seen, last_error,
        poll_enabled, path_hex, path_mode, voltage_correct)."""
        with self._lock:
            return self._conn.execute(
                "SELECT pubkey, name, guest_pw, last_seen, last_error,"
                " poll_enabled, path_hex, path_mode, voltage_correct"
                " FROM repeaters ORDER BY name, pubkey").fetchall()

    def repeater(self, pubkey):
        with self._lock:
            return self._conn.execute(
                "SELECT pubkey, name, guest_pw, last_seen, last_error,"
                " poll_enabled, path_hex, path_mode, voltage_correct"
                " FROM repeaters WHERE pubkey=?", (pubkey,)).fetchone()

    def set_poll_enabled(self, pubkey, on):
        with self._lock:
            self._conn.execute(
                "UPDATE repeaters SET poll_enabled=? WHERE pubkey=?",
                (1 if on else 0, pubkey))
            self._conn.commit()

    def set_voltage_correct(self, pubkey, on):
        """Mark/unmark a repeater's voltage readings as trustworthy.
        Unchecked -> the repeater is excluded from voltage alert checks."""
        with self._lock:
            self._conn.execute(
                "UPDATE repeaters SET voltage_correct=? WHERE pubkey=?",
                (1 if on else 0, pubkey))
            self._conn.commit()

    def set_position(self, pubkey, lat, lon):
        """Store the repeater's last-advertised position (from the companion's
        contact sync). 0/0 means no fix and is ignored."""
        if not lat and not lon:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE repeaters SET lat=?, lon=? WHERE pubkey=?",
                (float(lat), float(lon), pubkey))
            self._conn.commit()

    def positions(self):
        """{pubkey: (lat, lon)} for repeaters with a known position."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pubkey, lat, lon FROM repeaters"
                " WHERE lat IS NOT NULL AND lat != 0").fetchall()
        return {pk: (la, lo) for pk, la, lo in rows}

    def set_poll_enabled_all(self, on):
        with self._lock:
            self._conn.execute("UPDATE repeaters SET poll_enabled=?",
                               (1 if on else 0,))
            self._conn.commit()

    def set_path(self, pubkey, path_hex, mode):
        """Remember the custom path + hash byte size for a repeater."""
        with self._lock:
            self._conn.execute(
                "UPDATE repeaters SET path_hex=?, path_mode=? WHERE pubkey=?",
                (path_hex, mode, pubkey))
            self._conn.commit()

    def get_path(self, pubkey):
        with self._lock:
            row = self._conn.execute(
                "SELECT path_hex, path_mode FROM repeaters WHERE pubkey=?",
                (pubkey,)).fetchone()
        return (row[0], row[1]) if row else ("", 0)

    def set_path2(self, pubkey, path_hex, mode):
        """Remember the SECONDARY path (poll tries 5-6 fallback route)."""
        with self._lock:
            self._conn.execute(
                "UPDATE repeaters SET path2_hex=?, path2_mode=? WHERE pubkey=?",
                (path_hex, mode, pubkey))
            self._conn.commit()

    def get_path2(self, pubkey):
        with self._lock:
            row = self._conn.execute(
                "SELECT path2_hex, path2_mode FROM repeaters WHERE pubkey=?",
                (pubkey,)).fetchone()
        return (row[0], row[1]) if row else ("", 0)

    # ---- alert state -----------------------------------------------------
    def get_alert_state(self, pubkey, kind="temp"):
        """(state, last_alert_ts) or (None, 0) if never alerted."""
        with self._lock:
            r = self._conn.execute(
                "SELECT state, last_alert FROM alert_state"
                " WHERE pubkey=? AND kind=?", (pubkey, kind)).fetchone()
        return (r[0], r[1]) if r else (None, 0)

    def set_alert_state(self, pubkey, kind, state, ts):
        with self._lock:
            self._conn.execute(
                "INSERT INTO alert_state (pubkey, kind, state, last_alert)"
                " VALUES (?,?,?,?) ON CONFLICT(pubkey, kind) DO UPDATE SET"
                " state=excluded.state, last_alert=excluded.last_alert",
                (pubkey, kind, state, ts))
            self._conn.commit()

    def forget_repeater(self, pubkey):
        """Remove the repeater, its samples, poll log and alert state."""
        with self._lock:
            self._conn.execute("DELETE FROM samples WHERE pubkey=?", (pubkey,))
            self._conn.execute("DELETE FROM poll_log WHERE pubkey=?", (pubkey,))
            self._conn.execute("DELETE FROM alert_state WHERE pubkey=?", (pubkey,))
            self._conn.execute("DELETE FROM repeaters WHERE pubkey=?", (pubkey,))
            self._conn.commit()

    # ---- dashboard queries --------------------------------------------------
    def latest_in_window(self, metric, since):
        """{pubkey: (ts, value)} — the newest sample per repeater for one
        metric within the window."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pubkey, ts, value FROM samples"
                " WHERE metric=? AND ts>=?", (metric, since)).fetchall()
        out = {}
        for pk, ts, v in rows:
            if pk not in out or ts > out[pk][0]:
                out[pk] = (ts, v)
        return out

    def series_since(self, metric, since):
        """{pubkey: [(ts, value)...]} — full sample series for one metric."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pubkey, ts, value FROM samples"
                " WHERE metric=? AND ts>=? ORDER BY ts",
                (metric, since)).fetchall()
        out = {}
        for pk, ts, v in rows:
            out.setdefault(pk, []).append((ts, v))
        return out

    def avg_in_window(self, metric, t0, t1, pubkeys):
        """{pubkey: avg(value)} for one metric between t0..t1, restricted to
        the given repeaters."""
        if not pubkeys:
            return {}
        qmarks = ",".join("?" * len(pubkeys))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT pubkey, AVG(value) FROM samples WHERE metric=?"
                f" AND ts>=? AND ts<? AND pubkey IN ({qmarks})"
                f" GROUP BY pubkey", (metric, t0, t1, *pubkeys)).fetchall()
        return {pk: avg for pk, avg in rows}

    def max_sample_ts(self, since):
        with self._lock:
            r = self._conn.execute(
                "SELECT MAX(ts) FROM samples WHERE ts>=?", (since,)).fetchone()
        return r[0]

    # ---- samples ---------------------------------------------------------
    def add_samples(self, pubkey, metrics, ts=None):
        ts = ts if ts is not None else time.time()
        rows = [(ts, pubkey, k, float(v)) for k, v in metrics.items()
                if isinstance(v, (int, float))]
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO samples (ts, pubkey, metric, value)"
                " VALUES (?,?,?,?)", rows)
            self._conn.commit()

    def samples(self, pubkey, metric, since=0.0, until=None, limit=20000):
        until = until if until is not None else time.time() + 60
        with self._lock:
            return self._conn.execute(
                "SELECT ts, value FROM samples WHERE pubkey=? AND metric=?"
                " AND ts>=? AND ts<=? ORDER BY ts LIMIT ?",
                (pubkey, metric, since, until, limit)).fetchall()

    def newest_sample(self, pubkey, metric):
        """(ts, value) of the newest sample for one metric, or None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT ts, value FROM samples WHERE pubkey=? AND metric=?"
                " ORDER BY ts DESC LIMIT 1", (pubkey, metric)).fetchone()
        return r if r else None

    def newest_sample_any(self, pubkey):
        """(ts, metric, value) of the newest sample of any metric, or None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT ts, metric, value FROM samples WHERE pubkey=?"
                " ORDER BY ts DESC LIMIT 1", (pubkey,)).fetchone()
        return r if r else None

    def latest_samples_all(self, metric):
        """{pubkey: newest value} for one metric — a single query (the UI
        table refresh used to run one query PER repeater PER metric)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pubkey, value, MAX(ts) FROM samples"
                " WHERE metric=? GROUP BY pubkey", (metric,)).fetchall()
        return {pk: v for pk, v, _ts in rows}

    def latest_with_ts(self, metric):
        """{pubkey: (ts, value)} — the newest sample per repeater for one
        metric, regardless of age (dashboard ranking + tiles)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pubkey, value, MAX(ts) FROM samples"
                " WHERE metric=? GROUP BY pubkey", (metric,)).fetchall()
        return {pk: (ts, v) for pk, v, ts in rows}

    def latest_samples_between(self, metric, t0, t1):
        """{pubkey: newest value} for one metric with ts in [t0, t1) —
        used for the dashboard's trend-vs-previous-window comparison."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pubkey, value, MAX(ts) FROM samples"
                " WHERE metric=? AND ts>=? AND ts<? GROUP BY pubkey",
                (metric, t0, t1)).fetchall()
        return {pk: v for pk, v, _ts in rows}

    def latest_sample_with_ts(self, pubkey, metric):
        """(ts, value) of the newest sample for a repeater+metric, or None."""
        with self._lock:
            r = self._conn.execute(
                "SELECT ts, value FROM samples WHERE pubkey=? AND metric=?"
                " ORDER BY ts DESC LIMIT 1", (pubkey, metric)).fetchone()
        return (r[0], r[1]) if r else None

    def set_utilization(self, pubkey, util_tx, util_rx):
        """Remember the latest computed TX/RX utilization (%) per repeater."""
        with self._lock:
            self._conn.execute(
                "UPDATE repeaters SET util_tx=?, util_rx=? WHERE pubkey=?",
                (util_tx, util_rx, pubkey))
            self._conn.commit()

    def utilizations(self):
        """{pubkey: (util_tx, util_rx)} — latest computed utilization."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pubkey, util_tx, util_rx FROM repeaters"
                " WHERE util_tx IS NOT NULL").fetchall()
        return {pk: (tx, rx) for pk, tx, rx in rows}

    def last_poll_all(self, kind):
        """{pubkey: (ts, ok, detail)} of the newest poll-log entry of one
        kind per repeater — a single query."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pubkey, ts, ok, detail, MAX(ts) FROM poll_log"
                " WHERE kind=? GROUP BY pubkey", (kind,)).fetchall()
        return {pk: (ts, ok, detail) for pk, ts, ok, detail, _mx in rows}

    def latest_sample(self, pubkey, metric, max_age=None):
        """Newest value for a metric (None if the repeater never reported it).
        ORDER BY ts DESC — the previous code fetched the OLDEST sample in the
        window, so the metric columns never updated after the first poll."""
        q = ("SELECT ts, value FROM samples WHERE pubkey=? AND metric=?")
        args = [pubkey, metric]
        if max_age is not None:
            q += " AND ts>=?"
            args.append(time.time() - max_age)
        q += " ORDER BY ts DESC LIMIT 1"
        with self._lock:
            r = self._conn.execute(q, args).fetchone()
        return r[-1] if r else None

    def metrics_for(self, pubkey):
        with self._lock:
            return [r[0] for r in self._conn.execute(
                "SELECT DISTINCT metric FROM samples WHERE pubkey=?"
                " ORDER BY metric", (pubkey,))]

    # ---- poll log --------------------------------------------------------
    def log_poll(self, pubkey, kind, ok, detail=""):
        with self._lock:
            self._conn.execute(
                "INSERT INTO poll_log (ts, pubkey, kind, ok, detail) VALUES (?,?,?,?,?)",
                (time.time(), pubkey, kind, 1 if ok else 0, detail[:400]))
            self._conn.execute(
                "DELETE FROM poll_log WHERE ts < ?", (time.time() - 86400 * 7,))
            self._conn.commit()

    def last_poll(self, pubkey, kind):
        with self._lock:
            return self._conn.execute(
                "SELECT ts, ok, detail FROM poll_log WHERE pubkey=? AND kind=?"
                " ORDER BY ts DESC LIMIT 1", (pubkey, kind)).fetchone()
