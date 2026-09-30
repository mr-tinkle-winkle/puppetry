"""
A minimal obs-websocket v5 client (OBS 28+ has the server built in:
Tools -> WebSocket Server Settings). Standard library only -- the
WebSocket framing is ~60 lines and avoids a new dependency for two jobs:

  * replay_buffer_seconds(): how long OBS's replay buffer is, so the
    layered replay-buffer file keeps exactly that much input history;
  * add_browser_sources(): "Add to OBS" -- creates (or updates) Browser
    Sources for the overlay pages in the current scene.

Not verified against a real OBS in the environment this was written in
(tested against a local fake server speaking the same protocol) -- see
the handoff notes.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import uuid


class ObsError(Exception):
    pass


class ObsClient:
    def __init__(self, host="127.0.0.1", port=4455, password="", timeout=3.0):
        self.host, self.port, self.password, self.timeout = host, int(port), password or "", timeout
        self.sock: socket.socket | None = None
        self._buf = b""

    # -- websocket framing ---------------------------------------------------
    def _send_frame(self, payload: bytes, opcode=0x1) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(head + mask + body)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ObsError("OBS closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv_message(self) -> str:
        parts = []
        while True:
            b0, b1 = self._read_exact(2)
            opcode, fin = b0 & 0x0F, b0 & 0x80
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b1 & 0x80 else None
            data = self._read_exact(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise ObsError("OBS closed the connection" + (f": {data[2:].decode(errors='replace')}" if len(data) > 2 else ""))
            if opcode == 0x9:
                self._send_frame(data, 0xA)
                continue
            if opcode == 0xA:
                continue
            parts.append(data)
            if fin:
                return b"".join(parts).decode("utf-8")

    # -- protocol ------------------------------------------------------------
    def connect(self) -> "ObsClient":
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise ObsError(f"can't reach OBS at {self.host}:{self.port} ({e.strerror or e}) -- is the "
                           f"WebSocket server on (Tools -> WebSocket Server Settings)?") from None
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n")
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ObsError("OBS closed the connection during the handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ObsError("OBS refused the WebSocket connection")
        hello = json.loads(self._recv_message())
        if hello.get("op") != 0:
            raise ObsError("unexpected first message from OBS")
        ident = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello.get("d", {}).get("authentication")
        if auth:
            if not self.password:
                raise ObsError("OBS wants a WebSocket password -- set it in Puppetry's OBS settings")
            secret = base64.b64encode(hashlib.sha256((self.password + auth["salt"]).encode()).digest())
            ident["authentication"] = base64.b64encode(
                hashlib.sha256(secret + auth["challenge"].encode()).digest()).decode()
        self._send_frame(json.dumps({"op": 1, "d": ident}).encode())
        try:
            msg = json.loads(self._recv_message())
        except ObsError as e:
            raise ObsError("OBS rejected the login (wrong WebSocket password?)") from e
        if msg.get("op") != 2:
            raise ObsError("OBS didn't accept the login")
        return self

    def close(self) -> None:
        if self.sock:
            try:
                self._send_frame(struct.pack("!H", 1000), 0x8)
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *_):
        self.close()

    def request(self, rtype: str, data: dict | None = None) -> dict:
        rid = uuid.uuid4().hex
        d = {"requestType": rtype, "requestId": rid}
        if data is not None:
            d["requestData"] = data
        self._send_frame(json.dumps({"op": 6, "d": d}).encode())
        while True:
            msg = json.loads(self._recv_message())
            if msg.get("op") == 7 and msg["d"].get("requestId") == rid:
                st = msg["d"].get("requestStatus", {})
                if not st.get("result"):
                    raise ObsError(f"{rtype} failed: {st.get('comment') or st.get('code')}")
                return msg["d"].get("responseData") or {}

    # -- the two jobs ----------------------------------------------------------
    def replay_buffer_seconds(self) -> "float | None":
        """The replay buffer's length. Asks the replay-buffer output itself
        first (its name is localized, so it's found by kind), then the
        profile's own setting (Simple or Advanced output mode)."""
        try:
            outs = self.request("GetOutputList").get("outputs", [])
            for o in outs:
                if o.get("outputKind") == "replay_buffer":
                    st = self.request("GetOutputSettings", {"outputName": o["outputName"]}).get("outputSettings", {})
                    if st.get("max_time_sec"):
                        return float(st["max_time_sec"])
        except ObsError:
            pass
        try:
            mode = self.request("GetProfileParameter", {"parameterCategory": "Output",
                                                        "parameterName": "Mode"}).get("parameterValue")
            cat = "AdvOut" if (mode or "").lower().startswith("adv") else "SimpleOutput"
            v = self.request("GetProfileParameter", {"parameterCategory": cat, "parameterName": "RecRBTime"})
            val = v.get("parameterValue") or v.get("defaultParameterValue")
            if val:
                return float(val)
        except ObsError:
            pass
        return None

    def add_browser_sources(self, sources: list) -> list:
        """sources: [{"name", "url", "width", "height"}]. Creates each as a
        Browser Source in the current program scene, or updates the URL and
        size of one that already exists (by name). -> [(name, "created"|"updated")]"""
        scene = self.request("GetCurrentProgramScene")
        scene_name = scene.get("currentProgramSceneName") or scene.get("sceneName")
        existing = {i.get("inputName") for i in self.request("GetInputList").get("inputs", [])}
        done = []
        for s in sources:
            settings = {"url": s["url"], "width": int(s["width"]), "height": int(s["height"]),
                        "css": "body { background-color: rgba(0, 0, 0, 0); margin: 0px auto; overflow: hidden; }",
                        "shutdown": False, "restart_when_active": False}
            if s["name"] in existing:
                self.request("SetInputSettings", {"inputName": s["name"], "inputSettings": settings, "overlay": True})
                done.append((s["name"], "updated"))
            else:
                self.request("CreateInput", {"sceneName": scene_name, "inputName": s["name"],
                                             "inputKind": "browser_source", "inputSettings": settings,
                                             "sceneItemEnabled": True})
                done.append((s["name"], "created"))
        return done
