"""
gtpu: REAL GTP-U wire codec (3GPP TS 29.281). Encodes/decodes genuine bytes on the wire for the
N3/S1-U/S5-U/F1-U user plane — this RETIRES the GTP-U-over-JSON simplification (adapters/udp_json's
former stance 1). Stdlib only (struct/json).

Header (TS 29.281 section 5.1), the mandatory 8 octets plus the optional 4:
  octet 1   flags: version(bits 8-6 = 001) | PT(bit 5 = 1, GTP) | E | S | PN
  octet 2   message type (Echo Request = 1, Echo Response = 2, G-PDU = 255 / 0xFF)
  octet 3-4 length: number of octets AFTER the mandatory 8-octet header (incl. any optional part)
  octet 5-8 TEID (Tunnel Endpoint Identifier, 32 bits)
  octet 9-12 (present iff any of E/S/PN set) sequence number(16) | N-PDU number(8) | next-ext(8)
Then the T-PDU (the tunnelled user packet).

The T-PDU here carries the owned stack's MODELED inner packet as JSON bytes: the GTP-U tunnel FRAMING
(header, TEID, message type) is now genuine wire, while the inner IP packet stays a labeled model
(charter: "stub the interface, never the idea"). A decoder that only knows TS 29.281 can parse the
header + TEID off the wire; the inner bytes are the payload it would hand up the stack.
"""

import json
import struct

VERSION_PT = 0x30          # version=1 (001 in bits 8-6) | PT=1 (bit 5): the G-PDU/echo flag base
FLAG_E = 0x04              # extension-header-flag
FLAG_S = 0x02              # sequence-number-flag
FLAG_PN = 0x01            # N-PDU-number-flag

G_PDU = 0xFF               # message type 255 (TS 29.281 Table 6.1-1)
ECHO_REQUEST = 1
ECHO_RESPONSE = 2

_ECHO_NAME = {ECHO_REQUEST: "EchoRequest", ECHO_RESPONSE: "EchoResponse"}
_ECHO_CODE = {v: k for k, v in _ECHO_NAME.items()}


def encode(msg):
    """Encode a user-plane message dict to real GTP-U bytes.

    G-PDU (default): dict is {"teid": <int>, "payload": <json-able>}, optional "seq": <int>.
    Echo:            dict is {"messageType": "EchoRequest"|"EchoResponse", "teid": <int>,
                             optional "payload"/"seq"}.
    """
    mt_name = msg.get("messageType")
    mtype = _ECHO_CODE.get(mt_name, G_PDU)
    teid = int(msg.get("teid", 0)) & 0xFFFFFFFF
    payload = msg.get("payload", None)
    tpdu = json.dumps(payload).encode() if payload is not None else b""

    seq = msg.get("seq")
    if seq is not None:
        flags = VERSION_PT | FLAG_S
        optional = struct.pack(">HBB", int(seq) & 0xFFFF, 0, 0)   # seq | N-PDU | next-ext-type
    else:
        flags = VERSION_PT
        optional = b""

    length = len(optional) + len(tpdu)
    return struct.pack(">BBHI", flags, mtype, length, teid) + optional + tpdu


def decode(data):
    """Decode real GTP-U bytes back to a message dict (the inverse of encode)."""
    if len(data) < 8:
        raise ValueError("short GTP-U datagram")
    flags, mtype, _length, teid = struct.unpack(">BBHI", data[:8])
    offset = 8
    seq = None
    if flags & (FLAG_E | FLAG_S | FLAG_PN):        # the optional 4 octets are present
        if len(data) < 12:
            raise ValueError("GTP-U optional part flagged but truncated")
        (seq_val,) = struct.unpack(">H", data[8:10])
        if flags & FLAG_S:
            seq = seq_val
        offset = 12
    tpdu = data[offset:]
    payload = json.loads(tpdu) if tpdu else None

    if mtype in _ECHO_NAME:
        out = {"messageType": _ECHO_NAME[mtype], "teid": teid}
    else:
        out = {"teid": teid}
    if payload is not None:
        out["payload"] = payload
    if seq is not None:
        out["seq"] = seq
    return out
