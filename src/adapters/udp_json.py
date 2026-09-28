"""
udp_json: UDP-datagram carrier for the protocols that are UDP on the real wire (PFCP on N4, GTP-U on
N3/S1-U/S5-U/F1-U, E2AP later). The user-plane and N4 legs now ride REAL BINARY: this carrier
dispatches, by the socket's own port, to the real wire codecs — adapters.pfcp (TS 29.244) for the
PFCP ports and adapters.gtpu (TS 29.281) for the GTP-U ports. Every other port (E2AP, the O-DU Uu
radio leg, the edge DN echo socket) stays JSON, per design stance 1. Real port numbers are kept:
PFCP 8805/8806, GTP-U 2152-2159 (all shifted together by TELCO_PORT_OFFSET). Stdlib only.

The codec sits UNDER the existing call: an NF still calls `server.send(dict, addr)` or
`udp_request(dict, addr)` exactly as before, and the bytes on the wire are now genuine GTP-U /
PFCP — so no NF (SMF, O-CU-UP, O-DU, ...) needed a source change to speak the real protocol.
"""

import json
import secrets
import socket
import threading

from adapters import gtpu, pfcp
from domain.netconfig import offset, udp_port, bind_host

# Which port speaks which wire codec. Built once from netconfig so TELCO_PORT_OFFSET shifts these
# with every other port. The two EPC GTP-U sockets (SGW S1-U/S5-U 2158, PGW S5-U 2159) are
# subsystem-local — not registered UDP names — so their offset formula is replicated here.
_PFCP_PORTS = set()
_GTPU_PORTS = set()


def _register(target, *names):
    for name in names:
        try:
            target.add(udp_port(name))
        except Exception:      # noqa: BLE001 — a profile without the name simply skips it
            pass


_register(_PFCP_PORTS, "n4_central", "n4_edge")
_register(_GTPU_PORTS, "gtpu_central", "gtpu_cuup", "f1u", "gtpu_edge")
_GTPU_PORTS.update({2158 + offset(), 2159 + offset()})   # SGW / PGW EPC user-plane sockets


def _codec_for_port(p):
    if p in _PFCP_PORTS:
        return pfcp
    if p in _GTPU_PORTS:
        return gtpu
    return None


def _encode(msg, codec):
    return codec.encode(msg) if codec is not None else json.dumps(msg).encode()


def _decode(data, codec):
    return codec.decode(data) if codec is not None else json.loads(data)


class UdpJsonServer:
    """One bound UDP socket with a handler(msg, addr, send) -> optional reply dict (sent to addr).
    Inbound bytes are decoded, and replies encoded, with the codec bound to THIS socket's port
    (PFCP / GTP-U / JSON). If the inbound message carries a seq (request/response style, as PFCP
    does), the reply inherits it."""

    def __init__(self, name, port, handler):
        self.name = name
        self.handler = handler
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Honour TELCO_BIND, exactly as the HTTP servers do via
        # netconfig.bind_host(). This was hardcoded to loopback, which is correct
        # on the single host these services were written for and FATAL in
        # Kubernetes: the HTTP surfaces bound 0.0.0.0 and looked healthy while
        # every UDP leg -- PFCP N4, GTP-U, the O-DU's fronthaul legs -- was
        # unreachable from any other pod.
        #
        # The visible symptom was the SMF hanging forever in ensure_association():
        # PFCP is UDP, so packets arrived at the UPF pod IP, found nothing
        # listening, and were silently dropped. No refusal, no log, no error. A
        # UPF that had been up for 27 hours had never held a session and never
        # could. This one line is why the 5G user plane could not form.
        self.sock.bind((bind_host(), port))
        self.codec = _codec_for_port(self.sock.getsockname()[1])
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()
        print(f"[{self.name}] udp listening on "
              f"{self.sock.getsockname()[0]}:{self.sock.getsockname()[1]}", flush=True)

    def send(self, msg, addr):
        self.sock.sendto(_encode(msg, self.codec), addr)

    def _loop(self):
        while True:
            data, addr = self.sock.recvfrom(65535)
            try:
                msg = _decode(data, self.codec)
            except Exception:      # noqa: BLE001 — a malformed/foreign datagram is dropped
                continue
            reply = self.handler(msg, addr, self.send)
            if reply is not None:
                if "seq" in msg:
                    reply.setdefault("seq", msg["seq"])
                self.send(reply, addr)


def udp_request(msg, addr, timeout=2.0, retries=3):
    """Request/response over UDP with sequence matching, like PFCP (TS 29.244 section 7.2.2). The
    wire codec is chosen from the DESTINATION port, so the SMF's existing N4 call marshals to real
    PFCP bytes without any change at the call site."""
    codec = _codec_for_port(addr[1])
    seq = secrets.randbelow(1 << 24)
    msg = dict(msg, seq=seq)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for _ in range(retries):
            sock.sendto(_encode(msg, codec), addr)
            try:
                while True:
                    data, _ = sock.recvfrom(65535)
                    try:
                        reply = _decode(data, codec)
                    except Exception:      # noqa: BLE001 — ignore junk, keep waiting for our seq
                        continue
                    if reply.get("seq") == seq:
                        return reply
            except socket.timeout:
                continue
        raise TimeoutError(f"no reply from {addr} for {msg.get('messageType')}")
    finally:
        sock.close()
