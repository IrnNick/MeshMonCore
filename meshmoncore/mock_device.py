"""Mock MeshCore companion radio (TCP) for testing MeshMon without hardware.

Emulates the companion protocol frames the meshcore Python library exchanges:
  host -> device:  APP_START(1), GET_CONTACTS(4), SEND_TXT_MSG(2),
                   SEND_LOGIN(26), BINARY_REQ(50)
  device -> host:  SELF_INFO(5), CONTACT_START(2)/CONTACT(3)/CONTACT_END(4),
                   MSG_SENT(6), CONTACT_MSG_RECV_V3(16), LOGIN_SUCCESS(0x85),
                   BINARY_RESPONSE(0x8C)

Run:  python3 -m meshmoncore.mock_device [port]
"""
import asyncio
import os
import struct
import sys
import time
import random

# frame types (host commands)
CMD_APP_START = 1
CMD_SEND_TXT = 2
CMD_GET_CONTACTS = 4
CMD_SEND_LOGIN = 26
CMD_BINARY_REQ = 50

# frame types (device responses)
SELF_INFO = 5
MSG_SENT = 6
CONTACT_MSG_RECV_V3 = 16
CONTACT_START = 2
CONTACT = 3
CONTACT_END = 4
LOGIN_SUCCESS = 0x85
BINARY_RESPONSE = 0x8C

REPEATERS = [
    {
        "name": "REP-Hill-Top",
        "pubkey": bytes(range(0xA0, 0xC0)),
        "lat": 42.7, "lon": 23.3,
        "bat_mv": 3980, "temp_c": 21.4, "rssi": -92, "snr": 7.5,
        "uptime": 86400 * 3 + 4211, "airtime": 1900, "rx_airtime": 5400,
    },
    {
        "name": "REP-Valley",
        "pubkey": bytes(range(0xB0, 0xD0)),
        "lat": 42.65, "lon": 23.4,
        "bat_mv": 3720, "temp_c": 18.9, "rssi": -105, "snr": 1.2,
        "uptime": 86400 * 11 + 999, "airtime": 800, "rx_airtime": 2100,
    },
]

GUEST_OK = "OK - logged in as guest"


def lpp_telemetry(rep):
    """MeshCore LPP: ch1/type116 voltage (2B, /100) + ch1/type103 temp (2B, /10)."""
    v = int(rep["bat_mv"] // 10)
    t = int(round(rep["temp_c"] * 10)) & 0xFFFF
    return bytes([1, 116]) + v.to_bytes(2, "big") + bytes([1, 103]) + t.to_bytes(2, "big")


def repeater_status(rep):
    """RepeaterStats packed struct, in the field order of parsing.parse_status."""
    b = bytearray()
    b += struct.pack("<H", rep["bat_mv"])
    b += struct.pack("<H", 0)                        # tx_queue_len
    b += struct.pack("<h", -108)                     # noise_floor
    b += struct.pack("<h", rep["rssi"])
    b += struct.pack("<I", random.randint(200, 400))  # nb_recv
    b += struct.pack("<I", random.randint(50, 90))    # nb_sent
    b += struct.pack("<I", rep["airtime"])
    b += struct.pack("<I", rep["uptime"])
    b += struct.pack("<I", 30)                        # sent_flood
    b += struct.pack("<I", 60)                        # sent_direct
    b += struct.pack("<I", 900)                       # recv_flood
    b += struct.pack("<I", 1200)                      # recv_direct
    b += struct.pack("<H", 2)                         # full_evts
    b += struct.pack("<h", int(rep["snr"] * 4))       # last_snr
    b += struct.pack("<H", 3)                         # direct_dups
    b += struct.pack("<H", 1)                         # flood_dups
    b += struct.pack("<I", rep["rx_airtime"])
    b += struct.pack("<I", 0)                         # recv_errors
    return bytes(b)


def frame(pkt_type: int, payload: bytes) -> bytes:
    """Device->host frame: 0x3E + u16 LE size + data (type byte first).

    size counts the data bytes including the type byte (per the companion
    firmware / meshcore serial+tcp transports)."""
    return b"\x3e" + struct.pack("<H", len(payload) + 1) + bytes([pkt_type]) + payload


def msg_sent(txt_type: int) -> bytes:
    return msg_sent_with_tag(random.randint(1, 0xFFFFFF).to_bytes(4, "little"),
                             txt_type)


def msg_sent_with_tag(tag: bytes, txt_type: int) -> bytes:
    return frame(MSG_SENT, bytes([txt_type]) + tag
                 + struct.pack("<I", 4000))


class MockCompanion:
    def __init__(self, port):
        self.port = port
        self.pending = []
        # MESH_MOCK_DROP=N: silently drop the first N binary requests per
        # connection (lets tests exercise the retry + flood-fallback path)
        self.drop_left = int(os.environ.get("MESH_MOCK_DROP", "0"))
        # MESH_MOCK_HIDE_FIRST=N: omit REP-Hill-Top from the first N contact
        # syncs (simulates a repeater coming into range mid-session)
        self.hide_left = int(os.environ.get("MESH_MOCK_HIDE_FIRST", "0"))
        # MESH_MOCK_DEAD_PATH: models unreachable paths. "1" (legacy) makes
        # every CUSTOM path dead; a comma list ("0102,0304") makes exactly
        # those path hexes dead. Paths not listed always work, so tests can
        # prove the secondary-path fallback (primary dead, secondary alive).
        raw = os.environ.get("MESH_MOCK_DEAD_PATH", "")
        self.dead_paths = (None if raw == "1"
                           else {h.strip().lower()
                                 for h in raw.split(",") if h.strip()})
        self.path_state = {}   # pubkey32 -> active path hex ("" = mesh-managed)
        # real repeaters answer BINARY_REQ only from senders in their client
        # DB, and the only way in is a login (empty pw = guest) — model that
        self.logged_in = set()

    async def handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        print(f"mock: connection from {peer}", flush=True)
        buf = b""
        try:
            while True:
                chunk = await reader.read(512)
                if not chunk:
                    break
                buf += chunk
                # host frames: 0x3C + u16 LE size + data (data[0] = command type)
                while len(buf) >= 3 and buf[0] == 0x3C:
                    (sz,) = struct.unpack("<H", buf[1:3])
                    if len(buf) < 3 + sz:
                        break
                    pkt_type = buf[3]
                    payload = buf[4:3 + sz]
                    buf = buf[3 + sz:]
                    print(f"mock: frame type={pkt_type} len={len(payload)}",
                          flush=True)
                    await self.dispatch(pkt_type, payload, writer)
                    await self.push_pending(writer)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            print("mock: disconnected")
        finally:
            writer.close()

    def _find(self, pubkey32):
        for r in REPEATERS:
            if r["pubkey"] == pubkey32:
                return r
        return REPEATERS[0]

    async def dispatch(self, pkt_type, payload, writer):
        now = int(time.time())

        if pkt_type == CMD_APP_START:
            print("mock: app start")
            p = (bytes([2])                     # adv_type: repeater-ish
                 + bytes([27]) + bytes([27])    # tx power, max power
                 + REPEATERS[0]["pubkey"]       # self pubkey (32)
                 + struct.pack("<i", 42700000) + struct.pack("<i", 23300000)
                 + bytes([0])                   # multi_acks
                 + bytes([0])                   # adv_loc_policy
                 + bytes([0])                   # telemetry_mode
                 + bytes([0])                   # manual_add_contacts
                 + struct.pack("<I", 868500)    # freq (kHz per lib: /1000)
                 + struct.pack("<I", 250)       # bw (kHz)
                 + bytes([7])                   # sf
                 + bytes([5])                   # cr
                 + b"MOCK-COMPANION")
            writer.write(frame(SELF_INFO, p))
            await writer.drain()

        elif pkt_type == CMD_GET_CONTACTS:
            visible = [r for r in REPEATERS
                       if not (r["pubkey"].startswith(b"a0") and self.hide_left > 0)]
            if self.hide_left > 0:
                self.hide_left -= 1
            writer.write(frame(CONTACT_START, struct.pack("<I", len(visible))))
            nowb = struct.pack("<I", now)
            for rep in visible:
                c = (rep["pubkey"]                      # 32B
                     + bytes([2])                       # type: REPEATER
                     + bytes([0x08])                    # flags
                     + bytes([255])                     # out_path_len: direct/flood
                     + bytes(64)                        # out_path (fixed 64)
                     + rep["name"].encode().ljust(32, b"\x00")
                     + nowb                             # last_advert
                     + struct.pack("<i", int(rep["lat"] * 1e6))
                     + struct.pack("<i", int(rep["lon"] * 1e6))
                     + nowb)                            # lastmod
                writer.write(frame(CONTACT, c))
            writer.write(frame(CONTACT_END, nowb))
            await writer.drain()

        elif pkt_type == CMD_SEND_TXT:
            txt_type = payload[0]
            dst32 = payload[6:38]
            text = payload[38:].decode("utf-8", "ignore")
            rep = self._find(dst32)
            print(f"mock: TXT type={txt_type} to {rep['name']}: {text!r}")
            writer.write(msg_sent(txt_type))
            if text.startswith("pass "):
                reply = GUEST_OK
            elif text == "get bat":
                reply = f"> battery: {rep['bat_mv']}mV"
            elif text == "get temp":
                reply = f"> temp: {rep['temp_c']}C"
            else:
                reply = "ERR: unknown command"
            m = (bytes([int(rep["rssi"]) & 0xFF])
                 + b"\x00\x00"
                 + rep["pubkey"][:6]
                 + bytes([255])                     # direct
                 + bytes([1])                       # txt_type: CLI_DATA
                 + struct.pack("<I", now)
                 + reply.encode())
            self.pending.append(frame(CONTACT_MSG_RECV_V3, m))
            await writer.drain()

        elif pkt_type == 9:              # ADD_UPDATE_CONTACT
            pk32 = payload[0:32]
            pb = payload[34]             # high 2 bits hash mode, low 6 length
            if pb == 255:
                active = ""              # no path (mesh-managed)
            else:
                n = pb & 0x3F
                hmode = pb >> 6
                active = (payload[35:35 + n * (hmode + 1)].hex()
                          if n else "")
            self.path_state[pk32] = active
            print(f"mock: contact update accepted (path: "
                  f"{active or 'mesh-managed'})", flush=True)
            writer.write(frame(0, b"\x00"))   # OK
            await writer.drain()

        elif pkt_type == 31:             # GET_CHANNEL
            idx = payload[0]
            chans = [(0, "weather"), (1, "test")]
            if idx >= len(chans):
                writer.write(frame(1, b"\x01"))   # ERROR -> end of list
            else:
                i, nm = chans[idx]
                body = bytes([i]) + nm.encode().ljust(32, b"\x00") + bytes(16)
                writer.write(frame(18, body))      # CHANNEL_INFO
            await writer.drain()

        elif pkt_type == 3:              # SEND_CHANNEL_TXT_MSG
            # payload: [0x00 reserved][chan_idx 1B][timestamp 4B][text...]
            chan = payload[1]
            text = payload[6:].decode("utf-8", "ignore")
            print(f"mock: CHANNEL #{chan} message: {text!r}", flush=True)
            writer.write(frame(0, b"\x00"))   # OK
            await writer.drain()

        elif pkt_type == 13:             # RESET_PATH
            pk32 = payload[0:32]
            self.path_state[pk32] = ""
            print("mock: RESET_PATH accepted -> path mesh-managed",
                  flush=True)
            writer.write(frame(0, b"\x00"))   # OK
            await writer.drain()

        elif pkt_type == CMD_SEND_LOGIN:
            dst32 = payload[0:32]
            pwd = payload[32:].decode("utf-8", "ignore")
            rep = self._find(dst32)
            print(f"mock: LOGIN to {rep['name']} pwd={pwd!r}")
            self.logged_in.add(dst32)
            tag = random.randint(1, 0xFFFFFF).to_bytes(4, "little")
            writer.write(msg_sent_with_tag(tag, 0))
            self.pending.append(frame(LOGIN_SUCCESS,
                                      bytes([0]) + rep["pubkey"][:6]))
            await writer.drain()

        elif pkt_type == CMD_BINARY_REQ:
            pk32 = payload[0:32]
            if pk32 not in self.logged_in:
                # not in the client DB: the real repeater drops these silently
                print(f"mock: BINARY_REQ dropped — sender never logged in "
                      f"({pk32[:8]})", flush=True)
                await writer.drain()
                return
            active = self.path_state.get(pk32, "")
            if ((self.dead_paths is None and active)
                    or (self.dead_paths and active in self.dead_paths)):
                # custom path is dead: MSG_SENT but the repeater never answers
                tag = random.randint(1, 0xFFFFFF).to_bytes(4, "little")
                writer.write(frame(6, bytes([1]) + tag
                                   + struct.pack("<I", 4000)))
                print(f"mock: DEAD PATH drop for {pk32[:8]}", flush=True)
                await writer.drain()
                return
            if self.drop_left > 0:
                self.drop_left -= 1
                print(f"mock: DROP binary req ({self.drop_left} left)",
                      flush=True)
                # realistic drop: the companion ACCEPTS the message (MSG_SENT
                # with a random tag) but the repeater never answers -> the
                # request times out on the MeshMon side
                tag = random.randint(1, 0xFFFFFF).to_bytes(4, "little")
                writer.write(frame(6, bytes([1]) + tag
                                   + struct.pack("<I", 4000)))
                await writer.drain()
                return
            dst32 = payload[0:32]
            req = payload[32]
            rep = self._find(dst32)
            tag = random.randint(1, 0xFFFFFF).to_bytes(4, "little")
            # NB: the tag in BINARY_RESPONSE must EQUAL the expected_ack in
            # MSG_SENT — the library correlates the response via that tag.
            writer.write(msg_sent_with_tag(tag, 1))
            if req == 0x01:      # STATUS
                body = repeater_status(rep)
                self.pending.append(frame(BINARY_RESPONSE,
                                          bytes([req]) + tag + body))
                print(f"mock: STATUS req -> {rep['name']}")
            elif req == 0x03:    # TELEMETRY
                body = lpp_telemetry(rep)
                self.pending.append(frame(BINARY_RESPONSE,
                                          bytes([req]) + tag + body))
                print(f"mock: TELEMETRY req -> {rep['name']}")
            await writer.drain()

    async def push_pending(self, writer):
        while self.pending:
            f = self.pending.pop(0)
            await asyncio.sleep(0.3)
            writer.write(f)
            await writer.drain()


INSTANCE = None   # module-global handle for tests


def main(port=None):
    global INSTANCE
    if port is None:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 5060

    comp = MockCompanion(port)
    INSTANCE = comp

    async def run():
        srv = await asyncio.start_server(comp.handle,
                                         "127.0.0.1", port)
        print(f"mock companion listening on 127.0.0.1:{port}", flush=True)
        async with srv:
            await srv.serve_forever()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
