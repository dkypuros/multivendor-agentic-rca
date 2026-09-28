"""
adapters/milenage.py — REAL MILENAGE + REAL 5G/EPS key derivation for the owned core.

FIDELITY axis (charter section IV): this module PROMOTES the labeled sha256 authentication
stand-ins (udm.expected_res_star/derive_kausf, ausf.derive_kseaf, hss4g.xres/kasme, and the UE
res_star helpers) to standards-correct cryptography. It is clean-room and STDLIB-ONLY: AES-128 is
implemented here from scratch (no third-party crypto), and the key-derivation KDF uses the stdlib
hmac + hashlib.sha256. Nothing here reaches outside the standard library, so the co-located
container image builds and runs unchanged (charter PURITY: the owned core is stdlib-only).

STANDARDS IMPLEMENTED (executed, not asserted — see tests/run_milenage_spec.py for the KATs):
  MILENAGE f1, f1*, f2, f3, f4, f5, f5*   3GPP TS 35.205 / TS 35.206 (the example AKA algorithm
                                          set built on AES-128 / Rijndael with operator field OP).
  OPc = OP XOR E_K[OP]                     TS 35.206 section 8.2 (subscriber-specific OPc from OP+K).
  AUTN = (SQN XOR AK) || AMF || MAC-A      TS 33.102 section 6.3.2 (authentication token).
  5G key hierarchy KDF                     TS 33.501 Annex A (generic KDF of TS 33.220 Annex B.2:
    KAUSF  A.2   (FC 0x6A)                 HMAC-SHA256, FC || P0||L0 || P1||L1 ...).
    RES*   A.4   (FC 0x6B, low 128 bits)
    KSEAF  A.6   (FC 0x6C)
    KAMF   A.7   (FC 0x6D)
  EPS KASME                                TS 33.401 Annex A.2 (FC 0x10) — the 4G anchor key.

The operator variant OP is a network-wide constant (below). The permanent key K is the
subscriber's (services/core/udm/subscribers.json, services/core/hss4g/eps_subscribers.json). The
seed subscriber #1 key is the 3GPP TS 35.208 Test Set 1 key, so this module reproduces the
published TS 35.208 f1..f5 vectors EXACTLY — that is the vector-authenticity proof.

Author: clean-room from TS 35.206 (the algorithm) and TS 33.501/33.401 (the KDFs). No code copied.
"""

import hashlib
import hmac

# --------------------------------------------------------------------------- operator constant
# OP is the operator variant algorithm configuration field (TS 35.206 4.1): one value for the
# whole PLMN. This lab uses the 3GPP TS 35.208 Test Set 1 OP so that seed subscriber #1 (whose K
# is the Test Set 1 key) reproduces the published MILENAGE test vectors bit-for-bit. OPc is then
# DERIVED per subscriber as OP XOR E_K[OP] (TS 35.206 8.2) — never hard-coded per SIM.
OP = bytes.fromhex("cdc202d5123e20f62b6d676ac72cb318")   # TS 35.208 Test Set 1 OP

# MILENAGE rotation and XOR constants (TS 35.206 section 4.1, r1..r5 / c1..c5).
_R = (64, 0, 32, 64, 96)
_C = (
    bytes.fromhex("00000000000000000000000000000000"),          # c1
    bytes.fromhex("00000000000000000000000000000001"),          # c2
    bytes.fromhex("00000000000000000000000000000002"),          # c3
    bytes.fromhex("00000000000000000000000000000004"),          # c4
    bytes.fromhex("00000000000000000000000000000008"),          # c5
)


# =========================================================================== AES-128 (stdlib only)
# A from-scratch AES-128 block cipher (FIPS-197). Encryption of a single 16-byte block is all
# MILENAGE needs (E_K[X]). Clean-room, no third-party crypto — the charter forbids crypto deps in
# the owned core, and hashlib gives us SHA-256 but not AES, so AES is authored here.
_SBOX = None
_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36)


def _build_sbox():
    """Rijndael S-box via the standard GF(2^8) inverse + affine transform (FIPS-197 5.1.1)."""
    p = q = 1
    inv = [0] * 256
    # Compute multiplicative inverses using the (3=0x03) generator log/antilog walk.
    while True:
        # p = p * 3
        p = p ^ (p << 1) ^ (0x1B if p & 0x80 else 0)
        p &= 0xFF
        # q = q / 3 (three divisions by the generator)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        inv[p] = q
        if p == 1:
            break
    sbox = [0] * 256
    sbox[0] = 0x63
    for i in range(256):
        x = inv[i] if i != 0 else 0
        s = x
        for _ in range(4):
            x = ((x << 1) | (x >> 7)) & 0xFF
            s ^= x
        s ^= 0x63
        sbox[i] = s & 0xFF
    return sbox


def _sbox():
    global _SBOX
    if _SBOX is None:
        _SBOX = _build_sbox()
    return _SBOX


def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1)


def _mul(a, b):
    """Multiply in GF(2^8)."""
    res = 0
    for _ in range(8):
        if b & 1:
            res ^= a
        b >>= 1
        a = _xtime(a)
    return res & 0xFF


def _key_expansion(key):
    sbox = _sbox()
    words = [list(key[i:i + 4]) for i in range(0, 16, 4)]   # 4 words of the 128-bit key
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                       # RotWord
            temp = [sbox[b] for b in temp]                   # SubWord
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    # Group into 11 round keys of 16 bytes each.
    return [bytes(words[4 * r + c][b] for c in range(4) for b in range(4))
            for r in range(11)]


def aes128_encrypt(key, block):
    """AES-128 encrypt one 16-byte block (FIPS-197). key, block: 16-byte bytes. Returns 16 bytes."""
    if len(key) != 16 or len(block) != 16:
        raise ValueError("aes128_encrypt requires 16-byte key and block")
    sbox = _sbox()
    round_keys = _key_expansion(key)
    # State is column-major (state[r][c] = block[c*4 + r]).
    state = [[block[c * 4 + r] for c in range(4)] for r in range(4)]

    def add_round_key(rk):
        for c in range(4):
            for r in range(4):
                state[r][c] ^= rk[c * 4 + r]

    add_round_key(round_keys[0])
    for rnd in range(1, 10):
        # SubBytes
        for r in range(4):
            for c in range(4):
                state[r][c] = sbox[state[r][c]]
        # ShiftRows
        for r in range(1, 4):
            row = state[r]
            state[r] = row[r:] + row[:r]
        # MixColumns
        for c in range(4):
            a0, a1, a2, a3 = (state[r][c] for r in range(4))
            state[0][c] = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
            state[1][c] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
            state[2][c] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
            state[3][c] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)
        add_round_key(round_keys[rnd])
    # Final round (no MixColumns)
    for r in range(4):
        for c in range(4):
            state[r][c] = sbox[state[r][c]]
    for r in range(1, 4):
        row = state[r]
        state[r] = row[r:] + row[:r]
    add_round_key(round_keys[10])
    return bytes(state[r][c] for c in range(4) for r in range(4))


# =========================================================================== MILENAGE primitives
def _xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def _rot(x, r):
    """Cyclically rotate the 128-bit value x left by r bits (r a multiple of 8 for MILENAGE)."""
    n = r // 8
    return x[n:] + x[:n]


def compute_opc(k, op=OP):
    """OPc = OP XOR E_K[OP] (TS 35.206 8.2). Subscriber-specific: depends on both OP and K."""
    return _xor(op, aes128_encrypt(k, op))


def _f_all(k, rand, sqn, amf, op=OP):
    """Compute the full MILENAGE output set for one (K, RAND, SQN, AMF). Returns a dict with
    mac_a (f1, 8B), mac_s (f1*, 8B), res (f2, 8B), ck (f3, 16B), ik (f4, 16B), ak (f5, 6B),
    ak_star (f5*, 6B). All per TS 35.206 section 4.1."""
    opc = compute_opc(k, op)
    temp = aes128_encrypt(k, _xor(rand, opc))                       # TEMP = E_K[RAND XOR OPc]

    # f1 / f1*: IN1 = SQN || AMF || SQN || AMF  (SQN 6B, AMF 2B -> 16B)
    in1 = sqn + amf + sqn + amf
    out1 = _xor(aes128_encrypt(k, _xor(temp, _xor(_rot(_xor(in1, opc), _R[0]), _C[0]))), opc)
    mac_a = out1[:8]
    mac_s = out1[8:]

    # f2 / f5: OUT2 = E_K[ rot(TEMP XOR OPc, r2) XOR c2 ] XOR OPc
    out2 = _xor(aes128_encrypt(k, _xor(_rot(_xor(temp, opc), _R[1]), _C[1])), opc)
    ak = out2[:6]
    res = out2[8:]

    # f3: CK
    out3 = _xor(aes128_encrypt(k, _xor(_rot(_xor(temp, opc), _R[2]), _C[2])), opc)
    ck = out3

    # f4: IK
    out4 = _xor(aes128_encrypt(k, _xor(_rot(_xor(temp, opc), _R[3]), _C[3])), opc)
    ik = out4

    # f5*: resync AK
    out5 = _xor(aes128_encrypt(k, _xor(_rot(_xor(temp, opc), _R[4]), _C[4])), opc)
    ak_star = out5[:6]

    return {"mac_a": mac_a, "mac_s": mac_s, "res": res,
            "ck": ck, "ik": ik, "ak": ak, "ak_star": ak_star}


# =========================================================================== TS 33.501 Annex A KDF
def _kdf(key, s):
    """The generic KDF (TS 33.220 B.2 / TS 33.501 Annex A): HMAC-SHA256(key, S)."""
    return hmac.new(key, s, hashlib.sha256).digest()


def _param(p):
    """One KDF parameter Pn followed by its 2-byte big-endian length Ln."""
    return p + len(p).to_bytes(2, "big")


def _snn_bytes(snn):
    return snn.encode() if isinstance(snn, str) else snn


def derive_kausf(ck, ik, snn, sqn_xor_ak):
    """KAUSF (TS 33.501 Annex A.2): FC=0x6A, P0=SNN, P1=SQN XOR AK. Key = CK||IK."""
    s = b"\x6a" + _param(_snn_bytes(snn)) + _param(sqn_xor_ak)
    return _kdf(ck + ik, s)


def derive_res_star(ck, ik, snn, rand, res):
    """RES* (TS 33.501 Annex A.4): FC=0x6B, P0=SNN, P1=RAND, P2=RES. Key = CK||IK.
    RES* is the 128 LEAST significant bits (last 16 bytes) of the KDF output."""
    s = b"\x6b" + _param(_snn_bytes(snn)) + _param(rand) + _param(res)
    return _kdf(ck + ik, s)[16:]


def derive_kseaf(kausf, snn):
    """KSEAF (TS 33.501 Annex A.6): FC=0x6C, P0=SNN. Key = KAUSF."""
    s = b"\x6c" + _param(_snn_bytes(snn))
    return _kdf(kausf, s)


def derive_kamf(kseaf, supi, abba=b"\x00\x00"):
    """KAMF (TS 33.501 Annex A.7): FC=0x6D, P0=SUPI, P1=ABBA. Key = KSEAF."""
    s = b"\x6d" + _param(supi.encode() if isinstance(supi, str) else supi) + _param(abba)
    return _kdf(kseaf, s)


def _sn_id_bcd(plmn):
    """Serving-network id as the 3-octet BCD PLMN (TS 24.008 10.5.1.3) for the KASME KDF."""
    d = [int(c) for c in plmn]
    mcc1, mcc2, mcc3 = d[0], d[1], d[2]
    if len(d) == 6:
        mnc1, mnc2, mnc3 = d[3], d[4], d[5]
    else:
        mnc1, mnc2, mnc3 = d[3], d[4], 0xF
    return bytes([(mcc2 << 4) | mcc1, (mnc3 << 4) | mcc3, (mnc2 << 4) | mnc1])


def derive_kasme(ck, ik, plmn, sqn_xor_ak):
    """KASME (TS 33.401 Annex A.2): FC=0x10, P0=SN id (3-octet BCD PLMN), P1=SQN XOR AK.
    Key = CK||IK. The 4G EPS anchor key."""
    s = b"\x10" + _param(_sn_id_bcd(plmn)) + _param(sqn_xor_ak)
    return _kdf(ck + ik, s)


# =========================================================================== AUTN + vector helpers
def build_autn(sqn, ak, amf, mac_a):
    """AUTN = (SQN XOR AK) || AMF || MAC-A (TS 33.102 6.3.2). 16 bytes: 6 + 2 + 8."""
    return _xor(sqn, ak) + amf + mac_a


def parse_autn(autn):
    """Split AUTN into (SQN XOR AK, AMF, MAC-A)."""
    return autn[:6], autn[6:8], autn[8:16]


def network_vector(k_hex, rand, sqn, amf, snn=None, op=OP):
    """The HOME-NETWORK side of AKA: from K + RAND + SQN + AMF, produce every field a 5G/4G auth
    vector needs. Returns a dict of hex strings:
      rand, autn, ck, ik, ak, res (f2/XRES), mac_a, and (when snn given) xres_star, kausf, kasme.
    The UDM/HSS call this; the UE calls ue_vector() with the SAME K/OP and recovers SQN from AUTN,
    so both ends compute identical RES/RES*/keys — real 5G-AKA / EPS-AKA."""
    k = bytes.fromhex(k_hex)
    f = _f_all(k, rand, sqn, amf, op)
    autn = build_autn(sqn, f["ak"], amf, f["mac_a"])
    sqn_xor_ak = _xor(sqn, f["ak"])
    out = {"rand": rand.hex(), "autn": autn.hex(), "ck": f["ck"].hex(), "ik": f["ik"].hex(),
           "ak": f["ak"].hex(), "res": f["res"].hex(), "mac_a": f["mac_a"].hex(),
           "sqn_xor_ak": sqn_xor_ak.hex()}
    if snn is not None:
        out["xres_star"] = derive_res_star(f["ck"], f["ik"], snn, rand, f["res"]).hex()
        out["kausf"] = derive_kausf(f["ck"], f["ik"], snn, sqn_xor_ak).hex()
    return out


class MacFailure(Exception):
    """The UE could not verify MAC-A in AUTN (TS 33.102 6.3.3): not a genuine network."""


def ue_vector(k_hex, rand_hex, autn_hex, snn=None, op=OP, raise_on_mac=True):
    """The UE side of AKA (TS 33.102 6.3.3 / TS 33.501 6.1.3.2): given the received RAND and AUTN,
    recover SQN via AK = f5, verify MAC-A = f1, then compute RES (f2), CK (f3), IK (f4) and — when
    snn is given — RES* (TS 33.501 A.4). When raise_on_mac is True (default) a MAC mismatch raises
    MacFailure (the UE rejects a forged network / wrong key); when False the computed values are
    returned with mac_ok=False so a caller can drive the network-side rejection instead. Returns
    the same-shaped dict as network_vector (plus sqn and mac_ok)."""
    k = bytes.fromhex(k_hex)
    rand = bytes.fromhex(rand_hex)
    autn = bytes.fromhex(autn_hex)
    sqn_xor_ak, amf, mac_a = parse_autn(autn)
    # Recompute with a provisional SQN recovered from AK; f5 does not depend on SQN.
    opc = compute_opc(k, op)
    temp = aes128_encrypt(k, _xor(rand, opc))
    out2 = _xor(aes128_encrypt(k, _xor(_rot(_xor(temp, opc), _R[1]), _C[1])), opc)
    ak = out2[:6]
    sqn = _xor(sqn_xor_ak, ak)
    f = _f_all(k, rand, sqn, amf, op)
    mac_ok = (f["mac_a"] == mac_a)
    if not mac_ok and raise_on_mac:
        raise MacFailure("MAC-A mismatch: AUTN not authenticated (wrong K/OPc or forged network)")
    out = {"rand": rand.hex(), "autn": autn.hex(), "ck": f["ck"].hex(), "ik": f["ik"].hex(),
           "ak": f["ak"].hex(), "res": f["res"].hex(), "mac_a": f["mac_a"].hex(),
           "sqn": sqn.hex(), "mac_ok": mac_ok}
    if snn is not None:
        out["res_star"] = derive_res_star(f["ck"], f["ik"], snn, rand, f["res"]).hex()
        out["kausf"] = derive_kausf(f["ck"], f["ik"], snn, _xor(sqn, ak)).hex()
    return out


# Default AMF (authentication management field) for generated vectors (TS 33.102): 0x8000 marks a
# 5G/EPS AKA vector ("separation bit"); the KATs below override it with the Test Set 1 value.
DEFAULT_AMF = bytes.fromhex("8000")
