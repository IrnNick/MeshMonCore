"""RadioWorker — owns the meshcore companion connection (asyncio) in a QThread.

Connects over TCP (e.g. a wadamesh/MeshCore companion at 192.168.x.x:5000) or
serial (/dev/ttyACM0 / /dev/ttyUSB0). Keeps one guest session per repeater:
login once with the stored guest password, then poll STATUS + TELEMETRY
binary requests (both guest-accessible per MeshCore repeater firmware).
Results are stored via Store and emitted as Qt signals.
"""
import asyncio
import logging
import os
import threading
import time

from PySide6.QtCore import QObject, Signal

from meshcore import MeshCore, EventType

log = logging.getLogger("meshmon.radio")

# contact["type"] value for repeaters (meshcore AdvType.REPEATER)
TYPE_REPEATER = 2


class RadioWorker(QObject):
    contacts_changed = Signal(list)          # [contact dicts]
    connected = Signal(bool, str)            # ok, detail
    poll_result = Signal(str, dict)          # pubkey hex, metrics
    poll_failed = Signal(str, str)           # pubkey hex, error
    poll_progress = Signal(str, int, int)    # pubkey hex, try#, max tries
    polling_started = Signal()               # a poll run began (Stop becomes useful)
    polling_finished = Signal()              # the run ended (ok or aborted)
    channels_changed = Signal(list)          # [(idx, name)] of the companion's channels
    log_line = Signal(str)

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.loop = None
        self.mc = None
        self._cx_conf = None          # ("tcp", host, port) | ("serial", path, baud)
        self._stop_evt = None
        self._cfg_evt = None          # set when a connection config arrives
        self.poll_interval = 900      # seconds between poll rounds
        self.poll_enabled = False
        self._login_ok = set()        # pubkeys logged-in this session
        self._missing_logged = set()  # repeaters already reported as missing
        self._poll_task = None
        self._abort_evt = None        # set while a poll run is in flight
        self._thread = None           # the owning thread object (for join)
        self.channels = []            # [(idx, name)] of the companion's channels

        # temperature + voltage alerting (sent to a MeshCore channel)
        self.alert_enabled = False
        self.alert_channel_idx = None
        self.alert_low = -15.0        # C
        self.alert_high = 60.0        # C
        self.alert_low_v = 3.30       # V
        self.alert_high_v = 4.30      # V
        self.alert_reminder_min = 360 # re-alert an ongoing condition after N minutes

        # polling schedule: "interval" (every poll_interval seconds) or "hours"
        # (a poll round at each of poll_hours_minutes - minutes-of-day entries)
        self.poll_schedule_mode = "interval"
        self.poll_hours_minutes = []  # minutes-of-day, e.g. [360, 1080]
        # monotonic start of the last poll round (None = none yet); anchors
        # the interval so enabling auto-poll or a wake cannot fire a round
        # before the set interval has actually elapsed
        self._last_poll_round = None

        # temperature-alert schedule switch (mutually exclusive):
        #   "every" -> a check round (and re-alert if out of range) every
        #              alert_every_h hours;
        #   "daily" -> one check round per day at alert_daily_minutes
        #              (minutes of day, e.g. 395 = 06:35)
        self.alert_schedule_mode = "every"
        self.alert_every_h = 6
        self.alert_daily_minutes = 6 * 60 + 35   # 06:35
        # monotonic start of the last alert check round (None = none yet);
        # anchors the "every N hours" delay so the countdown cannot be reset
        # by the loop's periodic delay re-evaluation
        self._last_alert_check = None

        self.SECONDARY_AT = 5   # poll attempts >= 5 use the SECONDARY path
        self.rep_gap = int(os.environ.get("MESHMON_REP_GAP", "60"))
        # seconds between polling two repeaters in a round — always,
        # success or failure

    # ---- Qt -> asyncio bridge -------------------------------------------
    def start(self, cx_conf):
        """(Re)start the worker with a new connection config."""
        self._cx_conf = cx_conf
        if self.loop and self._stop_evt and not self._stop_evt.is_set():
            # thread already alive and running (reconnect) — wake it with the
            # new config
            self.loop.call_soon_threadsafe(self._cfg_evt.set)
            return
        # a previous thread may still be tearing down after stop(): joining it
        # prevents two loops from racing over self.mc / the socket (the
        # "can't connect" bug when Connect was clicked right after Disconnect)
        self._join_thread()
        self._kick_thread()

    def _join_thread(self):
        t = self._thread
        if t is not None and t.is_alive() \
                and t is not threading.current_thread():
            t.join(timeout=5.0)
        self._thread = None
        self.loop = None

    def _kick_thread(self):
        t = threading.Thread(target=self._thread_main, daemon=True,
                             name="meshmon-radio")
        self._thread = t
        t.start()

    def stop(self):
        if self.loop and self._stop_evt:
            self.loop.call_soon_threadsafe(self._stop_evt.set)

    def wake_schedule(self):
        """UI -> radio: the alert schedule/toggle changed; wake the alert loop
        so it recomputes its next check immediately."""
        if self.loop and self._wake_evt:
            self.loop.call_soon_threadsafe(self._wake_evt.set)

    def wake_poll_schedule(self):
        """UI -> radio: the poll schedule/toggle changed; wake the poll loop
        so it recomputes its next round immediately (enabling auto-poll or
        shortening the interval takes effect without waiting out the old
        delay)."""
        if self.loop and self._poll_wake_evt:
            self.loop.call_soon_threadsafe(self._poll_wake_evt.set)

    def call_soon(self, coro):
        if self.loop:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    # ---- asyncio side ----------------------------------------------------
    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._stop_evt = asyncio.Event()
        self._cfg_evt = asyncio.Event()
        self._wake_evt = asyncio.Event()
        self._poll_wake_evt = asyncio.Event()
        try:
            self.loop.run_until_complete(self._main())
        except Exception:  # noqa: BLE001
            log.exception("radio thread died")
        finally:
            self.loop.close()
            self.loop = None

    async def _main(self):
        while not self._stop_evt.is_set():
            # Idle until a connection config is set (user pressed Connect).
            if self._cx_conf is None:
                try:
                    await asyncio.wait_for(self._cfg_evt.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
            self._cfg_evt.clear()
            try:
                await self._connect()
                await self._run_session()
            except Exception as e:  # noqa: BLE001
                log.exception("session error")
                self.log_line.emit(f"connection error: {e}")
                self.connected.emit(False, str(e))
            try:
                await asyncio.wait_for(self._stop_evt.wait(), timeout=5)
            except asyncio.TimeoutError:
                continue

    async def _connect(self):
        kind = self._cx_conf[0]
        if kind == "tcp":
            _, host, port = self._cx_conf
            from meshcore.tcp_cx import TCPConnection
            cx = TCPConnection(host, int(port))
            label = f"tcp:{host}:{port}"
        else:
            _, path, baud = self._cx_conf
            from meshcore.serial_cx import SerialConnection
            cx = SerialConnection(path, int(baud))
            label = f"serial:{path}"
        self.mc = MeshCore(cx, debug=bool(int(os.environ.get("MESHMON_DEBUG", "0"))))
        # A dead IP or half-open socket must never hang the radio loop for
        # the OS-default ~2 min — cap the whole handshake.
        try:
            await asyncio.wait_for(self.mc.connect(), timeout=15)
        except asyncio.TimeoutError:
            try:
                await self.mc.disconnect()
            except Exception:  # noqa: BLE001
                pass
            raise ConnectionError(
                f"companion handshake timed out after 15 s ({label})")
        # pull the contact list (companion downloads its known nodes)
        try:
            if hasattr(self.mc, "ensure_contacts"):
                await asyncio.wait_for(self.mc.ensure_contacts(), timeout=25)
            else:
                await asyncio.wait_for(
                    self.mc.commands.get_contacts(timeout=5), timeout=25)
        except Exception as e:  # noqa: BLE001
            self.log_line.emit(f"contact sync warning: {e}")
        # merge adverts the companion hears DURING the session: new contacts
        # arrive as pushes and the lib re-syncs incrementally (lastmod-based)
        self.mc.auto_update_contacts = True
        self.connected.emit(True, label)
        self.log_line.emit(f"connected: {label} — {len(self.mc._contacts)} contacts")
        await self._apply_stored_paths()
        await self._enumerate_channels()
        self.contacts_changed.emit(self.contacts())

    async def _apply_stored_paths(self):
        """Re-push each repeater's remembered custom path to the companion —
        a reboot/reinstall wipes the companion's stored paths, but MeshMon
        remembers them per repeater."""
        for (pubkey, name, _pw, _ls, _le, _on,
             path_hex, mode, _vc) in self.store.repeaters():
            if not path_hex:
                continue
            c = self.find_contact(pubkey)
            if c is None:
                continue
            try:
                res = await self.mc.commands.update_contact(
                    c, path=path_hex, path_hash_mode=mode)
                ok = bool(res) and res.type != EventType.ERROR
                self.log_line.emit(
                    f"path restored for {name or pubkey[:12]}: "
                    f"{path_hex} mode {mode} -> {'OK' if ok else 'rejected'}")
            except Exception as e:  # noqa: BLE001
                self.log_line.emit(f"path restore failed for {pubkey[:12]}: {e}")

    async def _run_session(self):
        self._poll_task = asyncio.ensure_future(self._poll_loop())
        self._alert_task = asyncio.ensure_future(self._alert_loop())
        while not self._stop_evt.is_set():
            try:
                # NB: .wait() coroutine is created fresh each iteration — a
                # wait_for() timeout cancels the task it wraps, so reusing one
                # task here would kill the session on the second iteration.
                await asyncio.wait_for(self._stop_evt.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        for task in (self._poll_task, self._alert_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        self._poll_task = None
        self._alert_task = None

    # ---- contacts --------------------------------------------------------
    def contacts(self):
        return list(self.mc._contacts.values()) if self.mc else []

    def find_contact(self, pubkey_hex):
        want = (pubkey_hex or "").lower()
        for c in self.contacts():
            if (c.get("public_key") or "").lower() == want:
                return c
        # adverts pushed mid-session that the incremental sync has not
        # merged yet still count as known
        pending = getattr(self.mc, "_pending_contacts", {}) if self.mc else {}
        for c in pending.values():
            if (c.get("public_key") or "").lower() == want:
                return c
        return None

    def discover_repeaters(self):
        """Register every repeater-type contact into the store (idempotent)."""
        n = 0
        for c in self.contacts():
            if c.get("type") == TYPE_REPEATER:
                pk = c.get("public_key")
                if pk:
                    self.store.upsert_repeater(pk, name=c.get("adv_name"),
                                               last_seen=c.get("last_seen"))
                    lat, lon = c.get("adv_lat") or 0, c.get("adv_lon") or 0
                    if lat or lon:
                        self.store.set_position(pk, lat, lon)
                    n += 1
        # also register adverts pushed mid-session that the sync has not
        # merged yet
        pend = getattr(self.mc, "_pending_contacts", {}) if self.mc else {}
        for c in pend.values():
            if c.get("type") == TYPE_REPEATER:
                pk = c.get("public_key")
                if pk and self.store.repeater(pk) is None:
                    self.store.upsert_repeater(pk, name=c.get("adv_name"),
                                               last_seen=c.get("last_seen"))
                    n += 1
        return n

    # ---- temperature-alert check schedule --------------------------------
    def _alert_check_delay(self):
        """Seconds until the next alert check, per the switch."""
        if self.alert_schedule_mode == "daily":
            lt = time.localtime()
            now_sec = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
            target_sec = self.alert_daily_minutes * 60
            if target_sec <= now_sec:
                target_sec += 86400          # tomorrow's slot
            return target_sec - now_sec
        # "every": N hours after the last round STARTED (full N hours when
        # none has run since enabling)
        if self._last_alert_check is None:
            return self.alert_every_h * 3600
        return max(0.0, self.alert_every_h * 3600
                   - (time.monotonic() - self._last_alert_check))

    async def _alert_loop(self):
        # alert rounds fire ONLY at the configured schedule (interval or time
        # of day) — enabling alerts or connecting must not start a round
        while not self._stop_evt.is_set():
            if not self.alert_enabled:
                # alerts off: idle until they are turned back on (the UI wakes
                # us) or the device disconnects
                try:
                    await asyncio.wait_for(self._wake_evt.wait(), timeout=5.0)
                    self._wake_evt.clear()
                except asyncio.TimeoutError:
                    pass
                continue
            if self._last_alert_check is None:
                # entering the enabled state: the interval counts from NOW
                # (the first check fires after one full interval, never
                # immediately — and never "never", which a constant delay
                # would cause)
                self._last_alert_check = time.monotonic()
            if self._abort_evt is not None:
                await asyncio.sleep(2)      # a poll round is in flight
                continue
            # wait until the scheduled check time; a wake (toggle or schedule
            # change) just re-evaluates the delay, it never fires a round
            while not self._stop_evt.is_set():
                delay = self._alert_check_delay()
                if delay <= 1:
                    break                   # due now -> run the round
                try:
                    await asyncio.wait_for(self._wake_evt.wait(),
                                           timeout=min(delay - 1, 5.0))
                    self._wake_evt.clear()
                except asyncio.TimeoutError:
                    pass
            if self._stop_evt.is_set():
                break
            try:
                await self._alert_check_round()
            except Exception as e:  # noqa: BLE001
                log.exception("alert check round failed")
                self.log_line.emit(f"alert check error: {e}")

    async def _alert_check_round(self, tries=6):
        """The alert-schedule round: every MARKED repeater, up to `tries`
        attempts each, alert thresholds evaluated. It is the SAME round
        machinery the manual/scheduled polling uses (_poll_run), so it logs
        "poll NAME: try N/M" per attempt, honors SECONDARY-path fallback and
        is stoppable via the Stop button like any other round."""
        self._last_alert_check = time.monotonic()   # anchor the next delay
        await self._poll_run(None, tries=tries, evaluate=True)

    # ---- channels + temperature alerting ---------------------------------
    async def _enumerate_channels(self):
        """Ask the companion for its channel list (idx 0.. until ERROR)."""
        self.channels = []
        for idx in range(8):
            try:
                res = await self.mc.commands.get_channel(idx)
                if res is None or res.type == EventType.ERROR:
                    break
                info = res.payload
                nm = (info.get("channel_name") or "").strip()
                if nm:
                    self.channels.append((idx, nm))
            except Exception:  # noqa: BLE001
                break
        self.channels_changed.emit(list(self.channels))
        self.log_line.emit(
            "channels: " + (", ".join(f"#{n}" for _i, n in self.channels)
                            if self.channels else "none"))

    async def _evaluate_alert(self, pubkey, metrics):
        """Temperature + voltage threshold alerting -> channel message.

        Each kind (temp / volt) is checked independently against its own
        below/above thresholds. Out of range -> channel message; the same
        state re-alerts only after alert_reminder_min (prevents per-round
        spam); returning to range is silent."""
        if not (self.alert_enabled and self.alert_channel_idx is not None):
            return
        entry = self.store.repeater(pubkey)
        name = (entry[1] if entry else None) or pubkey[:12]
        # voltage_correct=0 -> the owner flagged this repeater's voltage
        # readings as unreliable: exclude it from voltage alert checks
        # (temperature alerts still apply; history still stores the reading)
        voltage_ok = bool(entry[8]) if entry else True
        batt = metrics.get("battery_v")
        temp = metrics.get("temperature_c")

        if temp is not None:
            await self._check_alert_kind(pubkey, name, "temp", temp,
                                         self.alert_low, self.alert_high,
                                         "📢", "°C", batt)
        if batt is not None and voltage_ok:
            await self._check_alert_kind(pubkey, name, "volt", batt,
                                         self.alert_low_v, self.alert_high_v,
                                         "🔋", "V", None)

    async def _check_alert_kind(self, pubkey, name, kind, value, low, high,
                                emoji, unit, batt):
        if low <= value <= high:
            return          # in range — silent (no recovery messages)
        state = "low" if value < low else "high"
        limit = low if state == "low" else high
        prev, last_alert = self.store.get_alert_state(pubkey, kind)
        now = time.time()
        if state == prev and (now - (last_alert or 0)) < self.alert_reminder_min * 60:
            return          # same condition, reminder not due yet
        if kind == "volt":
            txt = (f"{emoji} ALERT {name}: battery {value:.2f}{unit} "
                   f"({'below' if state == 'low' else 'above'} {limit:.2f}{unit})")
        else:
            txt = (f"{emoji} ALERT {name}: {value:.1f}{unit} "
                   f"({'below' if state == 'low' else 'above'} {limit:g}{unit})"
                   + (f", bat {batt:.2f}V" if batt else ""))
        await self._send_channel_text(txt)
        self.store.set_alert_state(pubkey, kind, state, now)

    async def _send_channel_text(self, text):
        try:
            res = await self.mc.commands.send_chan_msg(
                self.alert_channel_idx, text)
            ok = bool(res) and res.type != EventType.ERROR
            self.log_line.emit(
                f"channel message {'sent' if ok else 'FAILED'}: {text}")
            self.store.log_poll(None, "channel_msg", ok, text)
        except Exception as e:  # noqa: BLE001
            self.log_line.emit(f"channel message failed: {e}")

    # ---- secondary path (poll tries 5-6) ----------------------------------
    async def _use_secondary_path(self, pubkey, contact):
        """Push the stored SECONDARY path onto the companion's contact for
        the fallback tries. The store is not changed; the primary path is
        re-applied after the attempt loop."""
        sec_hex, sec_mode = self.store.get_path2(pubkey)
        if not sec_hex:
            return False
        try:
            res = await asyncio.wait_for(
                self.mc.commands.update_contact(
                    contact, path=sec_hex, path_hash_mode=sec_mode),
                timeout=10)
            ok = bool(res) and res.type != EventType.ERROR
            if ok:
                # reflect the switch in the cached contact (same as the lib)
                contact["out_path"] = sec_hex
                contact["out_path_len"] = len(sec_hex) // (2 * (sec_mode + 1))
                contact["out_path_hash_mode"] = sec_mode
                self.log_line.emit(
                    f"{pubkey[:12]}\u2026 tries 5-6 via SECONDARY path"
                    f" ({len(sec_hex) // (2 * (sec_mode + 1))} hops)")
            return ok
        except Exception as e:  # noqa: BLE001
            self.log_line.emit(f"secondary path switch failed: {e}")
            return False

    async def _radio_restore_path(self, pubkey):
        """Re-apply the user's stored PRIMARY path (if any) after the
        secondary-path tries. No stored primary -> the device path was
        mesh-managed before, so reset it back to that state."""
        stored_hex, mode = self.store.get_path(pubkey)
        contact = self.find_contact(pubkey)
        if contact is None:
            return
        if not stored_hex:
            try:
                await asyncio.wait_for(
                    self.mc.commands.reset_path(contact["public_key"]),
                    timeout=10)
                self.log_line.emit(
                    f"{pubkey[:12]}… device path reset (mesh-managed)")
            except Exception as e:  # noqa: BLE001
                self.log_line.emit(f"restore of device path failed: {e}")
            return
        try:
            res = await asyncio.wait_for(
                self.mc.commands.update_contact(
                    contact, path=stored_hex, path_hash_mode=mode),
                timeout=10)
            ok = bool(res) and res.type != EventType.ERROR
            self.log_line.emit(
                f"{pubkey[:12]}\u2026 primary path restored"
                f" ({'OK' if ok else 'rejected'})")
        except Exception as e:  # noqa: BLE001
            self.log_line.emit(f"restore of primary path failed: {e}")

    async def _inter_repeater_gap(self, abort):
        """Pause between two repeaters in a round (airtime courtesy) —
        always, success or failure; abortable via Stop."""
        try:
            await asyncio.wait_for(abort.wait(), timeout=self.rep_gap)
        except asyncio.TimeoutError:
            pass

    # ---- polling ---------------------------------------------------------
    def _secs_until_next_scheduled(self):
        """Seconds to wait before the next poll round, per the schedule:
        'interval' -> poll_interval seconds; 'hours' -> the next occurrence
        of the configured times of day (poll_hours_minutes)."""
        if self.poll_schedule_mode == "hours" and self.poll_hours_minutes:
            lt = time.localtime()
            now_min = lt.tm_hour * 60 + lt.tm_min
            day_left = 24 * 60 - now_min - lt.tm_sec / 60.0
            future = [m for m in self.poll_hours_minutes if m > now_min]
            if future:
                return (min(future) - now_min) * 60 - lt.tm_sec
            # next occurrence is tomorrow's first slot
            first = min(self.poll_hours_minutes)
            return (day_left + first) * 60 - lt.tm_sec
        # interval: poll_interval seconds after the last round STARTED (full
        # interval when none has run yet — enabling auto-poll must not fire
        # a round immediately)
        if self._last_poll_round is None:
            return self.poll_interval
        return max(0.0, self.poll_interval
                   - (time.monotonic() - self._last_poll_round))

    async def _poll_loop(self):
        await asyncio.sleep(5)            # settle after connect
        while not self._stop_evt.is_set():
            if not self.poll_enabled:
                # auto-poll off: idle until it is turned on (the UI wakes us)
                try:
                    await asyncio.wait_for(self._poll_wake_evt.wait(),
                                           timeout=5.0)
                    self._poll_wake_evt.clear()
                except asyncio.TimeoutError:
                    pass
                continue
            if self._last_poll_round is None:
                # entering the enabled state: the interval counts from NOW
                # (a round fires after one full interval, never immediately)
                self._last_poll_round = time.monotonic()
            if self._abort_evt is not None:
                await asyncio.sleep(2)      # a manual/other round is in flight
                continue
            # wait until the scheduled round time; a wake (toggle or schedule
            # change) just recomputes the delay, it never fires a round
            while not self._stop_evt.is_set():
                delay = self._secs_until_next_scheduled()
                if delay <= 1:
                    break                   # due now -> run the round
                try:
                    await asyncio.wait_for(self._poll_wake_evt.wait(),
                                           timeout=min(delay - 1, 5.0))
                    self._poll_wake_evt.clear()
                except asyncio.TimeoutError:
                    pass
            if self._stop_evt.is_set():
                return
            # the scheduled auto-poll runs the SAME round machinery as
            # manual polling and alert checks (_poll_run): 6 tries per
            # repeater, "try N/M" progress in the log, stoppable via the
            # Stop button — data is collected silently (evaluate=False),
            # alert thresholds are NOT checked here (the alert schedule
            # has its own rounds)
            try:
                await self._poll_run(None, tries=6, evaluate=False)
            except Exception as e:  # noqa: BLE001
                log.exception("scheduled poll round failed")
                self.log_line.emit(f"poll error: {e}")

    async def _sync_contacts_safe(self):
        """Incremental contacts re-sync (lastmod-based: one small exchange
        when nothing changed). Keeps the table/poll targets fresh when a
        repeater comes into range mid-session."""
        if not self.mc:
            return
        try:
            before = len(self.mc._contacts)
            await asyncio.wait_for(self.mc.ensure_contacts(), timeout=20)
            added = len(self.mc._contacts) - before
            if added > 0:
                self.log_line.emit(f"contacts synced: +{added} new")
                self.contacts_changed.emit(self.contacts())
            # refresh stored positions: the contact dict carries each node's
            # last-advertised lat/lon (GPS or manually set on the repeater)
            for c in self.mc._contacts.values():
                pk = c.get("public_key")
                lat = c.get("adv_lat") or 0
                lon = c.get("adv_lon") or 0
                if (lat or lon) and self.store.repeater(pk) is not None:
                    self.store.set_position(pk, lat, lon)
        except Exception as e:  # noqa: BLE001
            self.log_line.emit(f"contact sync warning: {e}")

    async def poll_one(self, pubkey, contact, evaluate=True,
                       via_secondary=False):
        """One poll attempt (login-if-needed + STATUS + TELEMETRY).

        evaluate=False collects data silently (history polling) without
        running the temperature-alert check — used by the regular
        auto-poll; the dedicated alert-check rounds pass True.

        Returns (ok, metrics, errors): ok=True when at least one data point
        was collected."""
        if self.store.repeater(pubkey) is None:
            # deleted from the list while a round was running — do not
            # resurrect it via upsert_repeater
            return False, {}, ["repeater deleted from the list"]
        now = time.time()
        name = contact.get("adv_name")
        self.store.upsert_repeater(pubkey, name=name, last_seen=now)
        metrics = {}
        errors = []

        # guest login once per session — REQUIRED even when the repeater has
        # no guest password: MeshCore repeaters only answer STATUS/TELEMETRY
        # from senders in their client DB, and a login is the only way in.
        # An empty-password login IS the guest login (firmware matches ""
        # against its guest_password and grants PERM_ACL_GUEST); skipping it
        # makes the repeater silently drop every request, flood or not.
        entry = self.store.repeater(pubkey)
        pw = entry[2] if entry else ""
        if pubkey not in self._login_ok:
            try:
                res = await self.mc.commands.send_login_sync(contact, pw, timeout=0)
                ok = bool(res) and res.type == EventType.LOGIN_SUCCESS
                if ok:
                    self._login_ok.add(pubkey)
                self.store.log_poll(pubkey, "login", ok,
                                    "ok" if ok else "rejected")
                if not ok:
                    errors.append("guest login rejected")
            except Exception as e:  # noqa: BLE001
                self.store.log_poll(pubkey, "login", False, str(e))

        # STATUS — battery mV, rssi/snr, airtime, uptime, packet counters
        try:
            st = await self.mc.commands.req_status_sync(contact, timeout=0)
            if st:
                m = {}
                if st.get("bat"):
                    m["battery_v"] = st["bat"] / 1000.0
                for src, dst in (("last_rssi", "rssi"),
                                 ("noise_floor", "noise_floor"),
                                 ("last_snr", "snr"),
                                 ("airtime", "airtime_s"), ("uptime", "uptime_s"),
                                 ("rx_airtime", "rx_airtime_s"),
                                 ("sent_flood", "sent_flood"),
                                 ("sent_direct", "sent_direct"),
                                 ("recv_flood", "recv_flood"),
                                 ("recv_direct", "recv_direct"),
                                 ("direct_dups", "direct_dups"),
                                 ("flood_dups", "flood_dups"),
                                 ("recv_errors", "recv_errors")):
                    v = st.get(src)
                    if v is not None:
                        m[dst] = v
                metrics.update(m)
                self.store.log_poll(pubkey, "status", True)
            else:
                errors.append("status timeout")
                self.store.log_poll(pubkey, "status", False, "timeout")
        except Exception as e:  # noqa: BLE001
            errors.append(f"status: {e}")
            self.store.log_poll(pubkey, "status", False, str(e))

        # TELEMETRY — battery V + temperature (+ extra sensor channels)
        try:
            telem = await self.mc.commands.req_telemetry_sync(contact, timeout=0)
            lpp = telem.get("lpp") if isinstance(telem, dict) else telem
            if isinstance(lpp, list):
                for rec in lpp:
                    ch = rec.get("channel")
                    t = rec.get("type")
                    val = rec.get("value")
                    if isinstance(val, list) and len(val) == 1:
                        val = val[0]
                    elif isinstance(val, list):
                        val = float(sum(val)) / len(val)
                    try:
                        val = float(val)
                    except (TypeError, ValueError):
                        continue
                    if t in (116, "voltage") and ch == 1:   # battery (V)
                        metrics.setdefault("battery_v", val)
                    elif t in (103, "temperature"):         # temperature (C)
                        metrics["temperature_c"] = val
                    elif ch is not None and t is not None:
                        metrics[f"lpp_{ch}_{t}"] = val
                self.store.log_poll(pubkey, "telemetry", True)
            else:
                errors.append("telemetry timeout")
                self.store.log_poll(pubkey, "telemetry", False, "timeout")
        except Exception as e:  # noqa: BLE001
            errors.append(f"telemetry: {e}")
            self.store.log_poll(pubkey, "telemetry", False, str(e))

        self._compute_utilization(pubkey, metrics, now)
        self.store.add_samples(pubkey, metrics, ts=now)
        self.poll_result.emit(pubkey, metrics)
        if via_secondary:
            self.log_line.emit(
                f"poll {pubkey[:12]}… via SECONDARY path (fallback try)")
        else:
            path_desc = self.contact_path(pubkey)
            if path_desc:
                self.log_line.emit(f"poll {pubkey[:12]}… via path: {path_desc}")
        ok = bool(metrics)
        if errors and not ok:
            self.poll_failed.emit(pubkey, "; ".join(errors))
        if evaluate:
            try:
                await self._evaluate_alert(pubkey, metrics)
            except Exception as e:  # noqa: BLE001
                log.exception("alert evaluation failed")
                self.log_line.emit(f"alert evaluation failed: {e}")
        return ok, metrics, errors

    # ---- utilization -------------------------------------------------------
    def _compute_utilization(self, pubkey, metrics, now):
        """TX/RX channel utilization (%) over the interval since the previous
        STATUS poll: the delta of the cumulative airtime counters divided by
        the elapsed time. Stored as samples (for the dashboard) and in the
        repeaters table (for the main list). Skipped when the counters went
        backwards (the repeater rebooted) or the interval is too short."""
        prev_air = self.store.latest_sample_with_ts(pubkey, "airtime_s")
        prev_rx = self.store.latest_sample_with_ts(pubkey, "rx_airtime_s")
        if (prev_air is not None
                and metrics.get("airtime_s") is not None):
            ts_prev, val_prev = prev_air
            elapsed = now - ts_prev
            delta = metrics["airtime_s"] - val_prev
            if elapsed >= 5 and delta >= 0:
                metrics["util_tx"] = round(delta / elapsed * 100.0, 2)
        if (prev_rx is not None
                and metrics.get("rx_airtime_s") is not None):
            ts_prev, val_prev = prev_rx
            elapsed = now - ts_prev
            delta = metrics["rx_airtime_s"] - val_prev
            if elapsed >= 5 and delta >= 0:
                metrics["util_rx"] = round(delta / elapsed * 100.0, 2)
        self.store.set_utilization(pubkey, metrics.get("util_tx"),
                                   metrics.get("util_rx"))

    # ---- path helpers ----------------------------------------------------
    def contact_path(self, pubkey):
        """Human-readable path of a contact: 'direct' / 'N hops (hex)'. None if unknown."""
        c = self.find_contact(pubkey)
        if not c:
            return None
        plen = c.get("out_path_len", -1)
        if plen == 0:
            return "direct"                     # 0 hops, mesh-computed
        if plen in (255, -1):
            return None                         # no path known yet
        # each hop occupies (hash_mode + 1) bytes on the wire — show the full
        # path, not just the first byte of every hop
        hmode = max(int(c.get("out_path_hash_mode", 0) or 0), 0)
        shown = c.get("out_path", "")[: plen * (hmode + 1) * 2]
        return f"{plen} hop{'s' if plen != 1 else ''} ({shown})"

    def raw_path(self, pubkey):
        """(out_path hex, out_path_len, hash_mode) of a contact, or None."""
        c = self.find_contact(pubkey)
        if not c:
            return None
        return (c.get("out_path", ""), c.get("out_path_len", -1),
                c.get("out_path_hash_mode", 0))

    async def apply_path(self, pubkey, path_hex, hash_mode):
        """Set a custom path (list of per-hop hashes, hex) on the companion."""
        c = self.find_contact(pubkey)
        if not c:
            return False, "repeater not in contacts"
        try:
            res = await self.mc.commands.update_contact(
                c, path=path_hex, path_hash_mode=hash_mode)
            ok = bool(res) and res.type != EventType.ERROR
            detail = "path updated" if ok else "rejected by companion"
            if ok:
                self.store.set_path(pubkey, path_hex, hash_mode)
            self.store.log_poll(pubkey, "path", ok, detail)
            self.log_line.emit(f"path for {pubkey[:12]}…: {detail}")
            # refresh the UI immediately so the Hops column reflects the change
            self.contacts_changed.emit(self.contacts())
            return ok, detail
        except Exception as e:  # noqa: BLE001
            self.log_line.emit(f"path update failed: {e}")
            return False, str(e)

    async def clear_path(self, pubkey):
        """Reset the contact path to the mesh-computed one."""
        c = self.find_contact(pubkey)
        if not c:
            return False, "repeater not in contacts"
        try:
            res = await self.mc.commands.reset_path(c["public_key"])
            ok = bool(res) and res.type != EventType.ERROR
            if ok:
                self.store.set_path(pubkey, "", 0)
            self.store.log_poll(pubkey, "path", ok,
                                "cleared" if ok else "rejected")
            # refresh the UI immediately: the Hops cell now shows unknown
            self.contacts_changed.emit(self.contacts())
            return ok, "path cleared (mesh re-learns)"
        except Exception as e:  # noqa: BLE001
            return False, str(e)

    async def set_direct_path(self, pubkey):
        """Set an explicit 0-hop DIRECT path (update_contact with an empty
        path). Only sensible for a 1-hop neighbour."""
        c = self.find_contact(pubkey)
        if not c:
            return False, "repeater not in contacts"
        try:
            res = await asyncio.wait_for(
                self.mc.commands.update_contact(c, path="", path_hash_mode=0),
                timeout=10)
            ok = bool(res) and res.type != EventType.ERROR
            detail = "direct (0 hops)" if ok else "rejected by companion"
            if ok:
                self.store.set_path(pubkey, "", 0)
            self.store.log_poll(pubkey, "path", ok, detail)
            self.log_line.emit(f"path for {pubkey[:12]}…: {detail}")
            self.contacts_changed.emit(self.contacts())
            return ok, detail
        except Exception as e:  # noqa: BLE001
            self.log_line.emit(f"direct path failed: {e}")
            return False, str(e)

    def set_direct_path_async(self, pubkey):
        if self.mc and self.loop:
            self.call_soon(self.set_direct_path(pubkey))

    def set_path(self, pubkey, path_hex, hash_mode):
        if self.mc and self.loop:
            self.call_soon(self.apply_path(pubkey, path_hex, hash_mode))

    async def apply_path2(self, pubkey, path_hex, hash_mode):
        """Store the SECONDARY path (poll tries 5-6 fallback). The companion's
        contact is NOT changed — the secondary path is pushed onto it only
        while tries 5-6 run, and the primary is restored afterwards."""
        self.store.set_path2(pubkey, path_hex, hash_mode)
        if path_hex:
            self.log_line.emit(
                f"secondary path for {pubkey[:12]}… saved "
                f"({len(path_hex) // (2 * (hash_mode + 1))} hops, "
                "used on tries 5-6)")
        else:
            self.log_line.emit(
                f"secondary path for {pubkey[:12]}… cleared")
        self.contacts_changed.emit(self.contacts())
        return True, "saved"

    def set_path2(self, pubkey, path_hex, hash_mode):
        if self.mc and self.loop:
            self.call_soon(self.apply_path2(pubkey, path_hex, hash_mode))

    def clear_path2_async(self, pubkey):
        if self.mc and self.loop:
            self.call_soon(self.apply_path2(pubkey, "", 0))

    def clear_path_async(self, pubkey):
        if self.mc and self.loop:
            self.call_soon(self.clear_path(pubkey))

    # ---- manual actions --------------------------------------------------
    def poll_now(self, pubkey=None, tries=6):
        """Manual poll run: up to `tries` attempts per repeater, with live
        progress (poll_progress) and a working Stop (stop_poll). Manual runs
        also evaluate alert thresholds (evaluate=True)."""
        if not (self.mc and self.loop):
            self.log_line.emit("not connected")
            return
        if self._abort_evt is not None:
            self.log_line.emit("a poll round is already running — use Stop first")
            return
        self.call_soon(self._poll_run(pubkey, tries, evaluate=True))

    def stop_poll(self):
        """Abort the running poll round (manual, scheduled or alert)."""
        if self.loop and self._abort_evt:
            self.loop.call_soon_threadsafe(self._abort_evt.set)
            self.log_line.emit("stopping poll…")

    async def _poll_run(self, pubkey, tries=6, evaluate=False):
        """The ONE round implementation every poll path shares (manual,
        scheduled auto-poll and alert checks): up to `tries` attempts per
        repeater with poll_progress feedback, SECONDARY-path fallback on
        tries 5-6, abortable via _abort_evt (Stop button), rep_gap between
        repeaters. evaluate passes through to poll_one: True also checks the
        alert thresholds (manual + alert rounds), False collects history
        silently (scheduled auto-poll)."""
        abort = asyncio.Event()
        self._abort_evt = abort
        self._last_poll_round = time.monotonic()   # anchor the next delay
        self.polling_started.emit()
        try:
            await self._sync_contacts_safe()
            targets = []
            if pubkey:
                targets.append(pubkey)
            else:
                targets = [pk for (pk, _n, _pw, _ls, _le, on, _ph, _pm, _vc)
                           in self.store.repeaters() if on]
            if not targets:
                self.log_line.emit("no repeaters are marked for polling")
                return
            for t_idx, pk in enumerate(targets):
                if abort.is_set() or self._stop_evt.is_set():
                    break
                entry = self.store.repeater(pk)
                name = entry[1] if entry else pk[:12]
                done = False
                secondary_on = False
                ever_seen = False
                for attempt in range(1, tries + 1):
                    if abort.is_set() or self._stop_evt.is_set():
                        break
                    if self.store.repeater(pk) is None:
                        break          # deleted from the list mid-round
                    self.poll_progress.emit(pk, attempt, tries)
                    c = self.find_contact(pk)
                    if c is None:
                        # not in the companion's list YET: re-sync and use the
                        # remaining tries — the repeater may come into range
                        # (advert pushed) while the round is running
                        self.store.log_poll(pk, "poll", False,
                                            f"attempt {attempt}: not in"
                                            " companion list")
                        self.log_line.emit(
                            f"{name}: attempt {attempt}/{tries} — not in the"
                            " companion's contact list yet, re-syncing…")
                        await self._sync_contacts_safe()
                        c = self.find_contact(pk)
                    if c is None:
                        try:
                            await asyncio.wait_for(abort.wait(), timeout=2)
                            break                      # stop pressed
                        except asyncio.TimeoutError:
                            continue                   # next try
                    ever_seen = True
                    use_secondary = attempt >= self.SECONDARY_AT
                    if use_secondary and not secondary_on:
                        # tries 5-6 go via the SECONDARY path; the primary is
                        # re-applied after the attempt loop
                        if await self._use_secondary_path(pk, c):
                            secondary_on = True
                    try:
                        ok, _m, _e = await self.poll_one(
                            pk, c, evaluate=evaluate,
                            via_secondary=use_secondary)
                        if ok:
                            done = True
                            break
                    except Exception as e:  # noqa: BLE001
                        self.poll_failed.emit(pk, str(e))
                    try:
                        await asyncio.wait_for(abort.wait(), timeout=2)
                        break                      # stop pressed mid-wait
                    except asyncio.TimeoutError:
                        pass
                if secondary_on:
                    # the device contact goes back to the user's primary path
                    await self._radio_restore_path(pk)
                if not done and not abort.is_set(
                        ) and self.store.repeater(pk) is not None:
                    if ever_seen:
                        self.poll_failed.emit(pk,
                                              f"no response in {tries} tries")
                    else:
                        self.poll_failed.emit(
                            pk, "repeater never appeared in the companion's"
                                " contact list")
                if t_idx < len(targets) - 1:
                    await self._inter_repeater_gap(abort)
        finally:
            self._abort_evt = None
            self.polling_finished.emit()

    def save_guest_pw_and_login(self, pubkey, pw):
        self.store.set_guest_pw(pubkey, pw)
        self._login_ok.discard(pubkey)
        if pw:
            self.poll_now(pubkey)
