"""
pfcp: REAL PFCP wire codec (3GPP TS 29.244). Encodes/decodes genuine bytes on the wire for the N4
session-management interface between the SMF and the UPF — this RETIRES the PFCP-over-JSON
simplification (adapters/udp_json's former stance 1). Stdlib only (struct/json/hashlib).

Message header (TS 29.244 section 7.2.2):
  octet 1    flags: version(bits 8-6 = 001) | spare | MP | S (SEID present)
  octet 2    message type
  octet 3-4  message length (octets after octet 4)
  octet 5-12 SEID (present iff S=1; session messages carry it, node messages do not)
  next 3     sequence number      +   1 spare octet

Information Elements (TS 29.244 section 8.1.1) are TLV: Type(2) | Length(2) | Value. Grouped IEs
(Create PDR, PDI, Create FAR, Forwarding Parameters) nest child IEs in their Value. This module
carries the exact session program the SMF already sends as REAL TLV: Node ID, Cause, F-SEID,
Create PDR (PDR ID / PDI{Source Interface, F-TEID | UE IP} / FAR ID), Create FAR (FAR ID / Apply
Action / Forwarding Parameters{Destination Interface, Outer Header Creation}). The owned stack's
lab-specific tags (session/PDR S-NSSAI per TS 23.501, the PCF-authorized 5QI, and the human-readable
SEID label) ride as enterprise-scoped IEs so the round-trip is lossless while the standard IEs remain
independently decodable off the raw bytes.
"""

import hashlib
import json
import struct

# --- message types (TS 29.244 Table 7.3.1) -------------------------------------------------------
MT = {
    "AssociationSetupRequest": 5,
    "AssociationSetupResponse": 6,
    "SessionEstablishmentRequest": 50,
    "SessionEstablishmentResponse": 51,
}
MT_NAME = {v: k for k, v in MT.items()}
_SESSION_MESSAGES = {"SessionEstablishmentRequest", "SessionEstablishmentResponse"}

VERSION = 0x20             # version=1 (001 in bits 8-6)
S_FLAG = 0x01              # SEID-present flag (bit 1)

# --- IE types (TS 29.244 section 8.1.2) ----------------------------------------------------------
IE_CREATE_PDR = 1
IE_PDI = 2
IE_CREATE_FAR = 3
IE_FORWARDING_PARAMETERS = 4
IE_CAUSE = 19
IE_SOURCE_INTERFACE = 20
IE_F_TEID = 21
IE_DESTINATION_INTERFACE = 42
IE_APPLY_ACTION = 44
IE_PDR_ID = 56
IE_F_SEID = 57
IE_NODE_ID = 60
IE_OUTER_HEADER_CREATION = 84
IE_UE_IP_ADDRESS = 93
IE_FAR_ID = 108
# Enterprise-scoped IEs (type >= 32768, the vendor-specific range) for the owned stack's lab tags.
IE_SNSSAI = 32769          # S-NSSAI (TS 23.501 5.15): sst(1) [+ sd(3)]
IE_FIVEQI = 32770          # PCF-authorized 5QI (1 octet)
IE_SEID_LABEL = 32771      # human-readable SEID string ("<supi>-<psi>") the owned stack keys sessions on

CAUSE_ACCEPTED = 1
APPLY_FORW = 0x0002        # Apply Action FORW bit (TS 29.244 section 8.2.26)
SRC_ACCESS, SRC_CORE = 0, 1
DST_ACCESS, DST_CORE = 0, 1
OHC_GTPU_UDP_IPV4 = 0x0100   # Outer Header Creation description bit for GTP-U/UDP/IPv4


# --- low-level TLV helpers -----------------------------------------------------------------------
def _ie(ie_type, value):
    return struct.pack(">HH", ie_type, len(value)) + value


def _parse_ies(data):
    """Parse a byte string of TLV IEs into an ordered list of (type, value)."""
    out, i = [], 0
    while i + 4 <= len(data):
        ie_type, length = struct.unpack(">HH", data[i:i + 4])
        value = data[i + 4:i + 4 + length]
        out.append((ie_type, value))
        i += 4 + length
    return out


def _first(ies, ie_type):
    for t, v in ies:
        if t == ie_type:
            return v
    return None


def _derive_seid(label):
    """Real 64-bit SEID for the header/F-SEID, derived stably from the owned stack's session key."""
    return struct.unpack(">Q", hashlib.sha256(label.encode()).digest()[:8])[0]


# --- typed IE encoders ---------------------------------------------------------------------------
def _enc_snssai(snssai):
    sst = int(snssai.get("sst", 1)) & 0xFF
    value = struct.pack(">B", sst)
    if snssai.get("sd"):
        value += bytes.fromhex(snssai["sd"])[:3].rjust(3, b"\x00")
    return _ie(IE_SNSSAI, value)


def _dec_snssai(value):
    out = {"sst": value[0]}
    if len(value) >= 4:
        out["sd"] = value[1:4].hex()
    return out


def _enc_fseid(label):
    seid = _derive_seid(label)
    lb = label.encode()
    value = struct.pack(">BQ4s", 0x02, seid, b"\x7f\x00\x00\x01")   # flags V4 | SEID | 127.0.0.1
    value += struct.pack(">B", len(lb)) + lb                        # trailing human SEID label
    return _ie(IE_F_SEID, value)


def _dec_fseid(value):
    """Returns (seid_int, label). The 8-octet SEID is the real F-SEID field; the label is the
    owned stack key appended after the standard IPv4 address."""
    _flags, seid, _ipv4 = struct.unpack(">BQ4s", value[:13])
    label = ""
    if len(value) > 13:
        n = value[13]
        label = value[14:14 + n].decode()
    return seid, label


def _enc_create_pdr(pdr):
    inner = _ie(IE_PDR_ID, struct.pack(">H", pdr["pdrId"]))
    uplink = pdr["direction"] == "uplink"
    src = _ie(IE_SOURCE_INTERFACE, struct.pack(">B", SRC_ACCESS if uplink else SRC_CORE))
    if uplink:
        teid = int(pdr["match"]["teid"]) & 0xFFFFFFFF
        f_teid = _ie(IE_F_TEID, struct.pack(">BI4s", 0x01, teid, b"\x00\x00\x00\x00"))
        pdi = _ie(IE_PDI, src + f_teid)
    else:
        ip = bytes(int(o) for o in pdr["match"]["ueIp"].split("."))
        ue_ip = _ie(IE_UE_IP_ADDRESS, struct.pack(">B", 0x02) + ip)   # flags V4 | IPv4
        pdi = _ie(IE_PDI, src + ue_ip)
    inner += pdi
    inner += _ie(IE_FAR_ID, struct.pack(">I", pdr["farId"]))
    inner += _enc_snssai(pdr.get("sNssai") or {"sst": 1})
    if pdr.get("5qi") is not None:
        inner += _ie(IE_FIVEQI, struct.pack(">B", int(pdr["5qi"]) & 0xFF))
    return _ie(IE_CREATE_PDR, inner)


def _dec_create_pdr(value):
    ies = _parse_ies(value)
    (pdr_id,) = struct.unpack(">H", _first(ies, IE_PDR_ID))
    pdi = _parse_ies(_first(ies, IE_PDI))
    src = _first(pdi, IE_SOURCE_INTERFACE)[0]
    if src == SRC_ACCESS:
        direction = "uplink"
        _flags, teid, _ipv4 = struct.unpack(">BI4s", _first(pdi, IE_F_TEID)[:9])
        match = {"teid": teid}
    else:
        direction = "downlink"
        ue = _first(pdi, IE_UE_IP_ADDRESS)
        match = {"ueIp": ".".join(str(b) for b in ue[1:5])}
    (far_id,) = struct.unpack(">I", _first(ies, IE_FAR_ID))
    pdr = {"pdrId": pdr_id, "direction": direction, "match": match, "farId": far_id,
           "sNssai": _dec_snssai(_first(ies, IE_SNSSAI))}
    fiveqi = _first(ies, IE_FIVEQI)
    if fiveqi is not None:
        pdr["5qi"] = fiveqi[0]
    return pdr


def _enc_create_far(far):
    inner = _ie(IE_FAR_ID, struct.pack(">I", far["farId"]))
    action = APPLY_FORW if far.get("apply") == "FORWARD" else 0
    inner += _ie(IE_APPLY_ACTION, struct.pack(">H", action))
    dest = far["destination"]
    if isinstance(dest, dict):        # forward toward a GTP-U peer (the gNB N3 endpoint)
        gtpu = dest["gtpu"]
        ip = bytes(int(o) for o in gtpu["ip"].split("."))
        ohc = struct.pack(">HI", OHC_GTPU_UDP_IPV4, int(dest["teid"]) & 0xFFFFFFFF) + ip \
            + struct.pack(">H", int(gtpu["port"]))
        fp = _ie(IE_DESTINATION_INTERFACE, struct.pack(">B", DST_ACCESS)) \
            + _ie(IE_OUTER_HEADER_CREATION, ohc)
    else:                              # forward to the data network (the modeled N6 echo)
        fp = _ie(IE_DESTINATION_INTERFACE, struct.pack(">B", DST_CORE))
    inner += _ie(IE_FORWARDING_PARAMETERS, fp)
    return _ie(IE_CREATE_FAR, inner)


def _dec_create_far(value):
    ies = _parse_ies(value)
    (far_id,) = struct.unpack(">I", _first(ies, IE_FAR_ID))
    (action,) = struct.unpack(">H", _first(ies, IE_APPLY_ACTION))
    fp = _parse_ies(_first(ies, IE_FORWARDING_PARAMETERS))
    ohc = _first(fp, IE_OUTER_HEADER_CREATION)
    if ohc is not None:
        _desc, teid = struct.unpack(">HI", ohc[:6])
        ip = ".".join(str(b) for b in ohc[6:10])
        (port,) = struct.unpack(">H", ohc[10:12])
        destination = {"gtpu": {"ip": ip, "port": port}, "teid": teid}
    else:
        destination = "DN-echo"
    return {"farId": far_id, "apply": "FORWARD" if action & APPLY_FORW else "DROP",
            "destination": destination}


# --- header --------------------------------------------------------------------------------------
def _encode_header(mt_name, seid_label, seq, body_len):
    oct1 = VERSION | (S_FLAG if mt_name in _SESSION_MESSAGES else 0)
    seq3 = struct.pack(">I", int(seq) & 0xFFFFFF)[1:] + b"\x00"       # 3-octet seq + 1 spare
    if mt_name in _SESSION_MESSAGES:
        length = 8 + len(seq3) + body_len
        head = struct.pack(">BBH", oct1, MT[mt_name], length)
        head += struct.pack(">Q", _derive_seid(seid_label or ""))
    else:
        length = len(seq3) + body_len
        head = struct.pack(">BBH", oct1, MT[mt_name], length)
    return head + seq3


def _decode_header(data):
    oct1, mt_code, _length = struct.unpack(">BBH", data[:4])
    mt_name = MT_NAME.get(mt_code, f"Unknown({mt_code})")
    i = 4
    if oct1 & S_FLAG:
        i += 8            # skip the 8-octet SEID (the human label rides in the F-SEID IE)
    seq = struct.unpack(">I", b"\x00" + data[i:i + 3])[0]
    i += 4                # 3-octet seq + 1 spare
    return mt_name, seq, data[i:]


# --- public API ----------------------------------------------------------------------------------
def encode(msg):
    """Encode an N4 message dict to real PFCP bytes."""
    mt_name = msg["messageType"]
    seq = msg.get("seq", 0)
    body = b""
    if mt_name == "AssociationSetupRequest":
        body = _enc_node_id(msg["nodeId"])
    elif mt_name == "AssociationSetupResponse":
        body = _enc_node_id(msg["nodeId"]) + _enc_cause(msg.get("cause"))
    elif mt_name == "SessionEstablishmentRequest":
        body = _enc_fseid(msg["seid"]) + _enc_snssai(msg.get("sNssai") or {"sst": 1})
        for pdr in msg.get("pdrs", []):
            body += _enc_create_pdr(pdr)
        for far in msg.get("fars", []):
            body += _enc_create_far(far)
    elif mt_name == "SessionEstablishmentResponse":
        body = _enc_cause(msg.get("cause")) + _enc_fseid(msg.get("seid", ""))
    else:
        raise ValueError(f"pfcp: unsupported messageType {mt_name!r}")
    return _encode_header(mt_name, msg.get("seid"), seq, len(body)) + body


def decode(data):
    """Decode real PFCP bytes back to an N4 message dict (the inverse of encode)."""
    mt_name, seq, body = _decode_header(data)
    ies = _parse_ies(body)
    out = {"messageType": mt_name, "seq": seq}
    if mt_name in ("AssociationSetupRequest", "AssociationSetupResponse"):
        out["nodeId"] = _dec_node_id(_first(ies, IE_NODE_ID))
    if mt_name in ("AssociationSetupResponse", "SessionEstablishmentResponse"):
        out["cause"] = _dec_cause(_first(ies, IE_CAUSE))
    if mt_name in _SESSION_MESSAGES:
        _seid_int, label = _dec_fseid(_first(ies, IE_F_SEID))
        out["seid"] = label
    if mt_name == "SessionEstablishmentRequest":
        out["sNssai"] = _dec_snssai(_first(ies, IE_SNSSAI))
        out["pdrs"] = [_dec_create_pdr(v) for t, v in ies if t == IE_CREATE_PDR]
        out["fars"] = [_dec_create_far(v) for t, v in ies if t == IE_CREATE_FAR]
    return out


def _enc_node_id(node_id):
    return _ie(IE_NODE_ID, struct.pack(">B", 2) + node_id.encode())   # type 2 = FQDN


def _dec_node_id(value):
    return value[1:].decode() if value else None


def _enc_cause(cause):
    return _ie(IE_CAUSE, struct.pack(">B", CAUSE_ACCEPTED if cause == "ACCEPTED" else 0))


def _dec_cause(value):
    if value is None:
        return None
    return "ACCEPTED" if value[0] == CAUSE_ACCEPTED else f"CAUSE_{value[0]}"
