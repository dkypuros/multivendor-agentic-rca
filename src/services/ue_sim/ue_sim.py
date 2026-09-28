"""
UE simulator: attaches through the gNB only (P3). No direct path to the core remains.

RRC per TS 38.331 5.3.3: RRCSetupRequest -> RRCSetup -> RRCSetupComplete carrying the NAS
RegistrationRequest; subsequent NAS rides in ULInformationTransfer. NAS itself per TS 24.501:
registration (5.5.1), 5G-AKA answer (REAL RES* — the UE runs the SAME MILENAGE (TS 35.206) as the
UDM: it recovers SQN via f5, verifies MAC-A via f1, then derives RES/CK/IK and RES* per TS 33.501
Annex A.4, adapters/milenage.py), security mode (5.4.2), PDU session establishment (6.4.1) with dnn
and sNssai, accept carries the PDU address (9.11.4.10).

With --pdu-echo N: after registration and session setup, sends N echo-requests over the Uu user-plane
socket (JSON datagrams to the gNB, which owns the N3 GTP-U tunnel) and counts replies.

Network slicing wave 1 (epic #11):
  --nssai "1" / "1:000001" / "1,2:000001"   requested NSSAI in the RegistrationRequest
                                            (TS 24.501 9.11.3.37; canonical "SST[:SD]" strings,
                                            comma-separated). WITHOUT the flag no requestedNssai
                                            field is sent at all — the legacy NAS, byte-identical.
  --slice-echo N [--slice-nssai S] [--slice-dnn D]   a third PDU session (pduSessionId=3) on
                                            S-NSSAI S (default "1") and dnn D (default internet),
                                            then N echoes — the slice-driven-selection probe.

Usage: python3 ue_sim.py <supi> <k_hex> [--nssai LIST] [--pdu-echo N] [--edge-echo N]
                         [--slice-echo N [--slice-nssai S] [--slice-dnn D]]
Exit codes: 0 success, 2 rejected or failed.
"""

import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from adapters import milenage
from adapters.sbi_http import request
from domain import obs
from domain.netconfig import host, plmn, udp_port, url
from domain.snssai import parse as parse_snssai

GNB_RRC = url("odu") + "/rrc/ue-messages"
GNB_UU = (host(), udp_port("uu"))
# The serving network name the UE camps on — identical construction to the AMF's SERVING_NETWORK_NAME
# (TS 24.501 9.12.1). RES* (TS 33.501 A.4) binds to it, so the UE must use the same SNN as the UDM.
_MCC, _MNC = plmn()[:3], plmn()[3:]
SERVING_NETWORK_NAME = f"5G:mnc{_MNC.zfill(3)}.mcc{_MCC}.3gppnetwork.org"


def res_star(k, rand, autn):
    """REAL 5G-AKA RES* (TS 33.501 6.1.3.2 / Annex A.4). The UE runs MILENAGE over RAND+AUTN with
    its K/OP: recovers SQN (f5), verifies MAC-A (f1) against AUTN, derives RES/CK/IK, then RES* via
    the Annex A.4 KDF bound to the serving network name. On a MAC mismatch (wrong K / forged
    network) it still returns a value computed from its own key so the network-side check rejects
    it — the AUSF/UDM authoritatively fails the mismatch, exactly as before."""
    try:
        uev = milenage.ue_vector(k, rand, autn, snn=SERVING_NETWORK_NAME)
        return uev["res_star"]
    except milenage.MacFailure:
        # Wrong K: MAC-A did not verify. Compute RES* from this (wrong) key anyway so the network
        # rejects on the RES* mismatch and the reject path (metrics + NAS) is byte-identical.
        uev = milenage.ue_vector(k, rand, autn, snn=SERVING_NETWORK_NAME, raise_on_mac=False)
        return uev["res_star"]


def send(supi, rrc_type, nas=None):
    body = {"rrc": {"type": rrc_type, "supi": supi}}
    if nas is not None:
        body["nas"] = nas
    status, reply = request("POST", GNB_RRC, body)
    label = nas["type"] if nas else rrc_type
    reply_label = reply.get("nas", {}).get("type") or reply.get("rrc", {}).get("type")
    print(f"  UE -> gNB {label:32s} gNB -> UE {reply_label}", flush=True)
    return reply.get("nas", {}), reply.get("rrc", {})


def main(supi, k, requested_nssai=None):
    # Correlation (issue #27): the UE registration is an entry edge - mint an id here; it
    # rides every RRC/NAS HTTP hop (O-DU -> O-CU-CP -> AMF -> UDM) via X-Correlation-Id.
    obs.set_corr(obs.new_corr())
    obs.log("registration_start", supi=supi)
    print(f"[ue_sim] attach via gNB for {supi}", flush=True)
    nas, rrc = send(supi, "RRCSetupRequest")
    if rrc.get("type") != "RRCSetup":
        print("[ue_sim] RRC setup failed", flush=True)
        return 2

    # requestedNssai (TS 24.501 9.11.3.37) only when --nssai was given: a legacy invocation
    # sends the exact pre-slicing RegistrationRequest, field for field.
    reg_request = {"type": "RegistrationRequest", "supi": supi}
    if requested_nssai:
        reg_request["requestedNssai"] = requested_nssai
    nas, _ = send(supi, "RRCSetupComplete", reg_request)
    if nas.get("type") != "AuthenticationRequest":
        print(f"[ue_sim] REJECTED at registration: gmmCause={nas.get('gmmCause')} "
              f"({nas.get('causeText')})", flush=True)
        return 2

    answer = res_star(k, nas["rand"], nas["autn"])
    nas, _ = send(supi, "ULInformationTransfer",
                  {"type": "AuthenticationResponse", "supi": supi, "resStar": answer})
    if nas.get("type") != "SecurityModeCommand":
        print("[ue_sim] REJECTED at authentication", flush=True)
        return 2

    nas, _ = send(supi, "ULInformationTransfer", {"type": "SecurityModeComplete", "supi": supi})
    if nas.get("type") != "RegistrationAccept":
        print(f"[ue_sim] REJECTED at registration accept: gmmCause={nas.get('gmmCause')}",
              flush=True)
        return 2

    obs.log("registered", supi=supi)
    print(f"[ue_sim] REGISTERED guami={nas['guami']} allowedNssai={nas['allowedNssai']}", flush=True)
    return 0


def pdu_session_and_echo(supi, echo_count, dnn="internet", pdu_session_id=1, snssai=None):
    nas, _ = send(supi, "ULInformationTransfer",
                  {"type": "PduSessionEstablishmentRequest", "supi": supi,
                   "pduSessionId": pdu_session_id, "dnn": dnn,
                   "sNssai": snssai or {"sst": 1}})
    if nas.get("type") != "PduSessionEstablishmentAccept":
        print(f"[ue_sim] PDU SESSION REJECTED: {nas.get('causeText') or nas.get('cause')}",
              flush=True)
        return 2
    print(f"[ue_sim] PDU SESSION pduAddress={nas['pduAddress']} dnn={nas['dnn']} "
          f"sNssai={nas['sNssai']}", flush=True)

    uu = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # Loopback when the gNB is co-located (every spec); a routable source when TELCO_HOST names
    # a remote gNB (the ran-sandbox runs the UE in its own pod, the O-DU in the ran-slice pod).
    uu.bind(("127.0.0.1" if GNB_UU[0] in ("127.0.0.1", "localhost") else "0.0.0.0", 0))
    uu.settimeout(2.0)
    replies = 0
    served_by = set()
    for i in range(echo_count):
        payload = {"type": "echo-request", "id": i, "data": "ping-" * 8}
        uu.sendto(json.dumps({"supi": supi, "payload": payload}).encode(), GNB_UU)
        try:
            data, _ = uu.recvfrom(65535)
            reply = json.loads(data).get("payload", {})
            if reply.get("id") == i:
                replies += 1
                served_by.add(reply.get("servedBy", "core-dn"))
        except socket.timeout:
            pass
    tag = f" servedBy={sorted(served_by)}" if served_by else ""
    print(f"[ue_sim] ECHO {replies}/{echo_count} replies via the RAN over the GTP-U tunnel"
          f" (dnn={dnn}){tag}", flush=True)
    return 0 if replies == echo_count else 2


def flag(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


if __name__ == "__main__":
    obs.init("ue_sim")
    nssai_arg = flag("--nssai")
    requested = [parse_snssai(tok) for tok in nssai_arg.split(",") if tok] if nssai_arg else None
    rc = main(sys.argv[1], sys.argv[2], requested)
    if rc == 0 and "--pdu-echo" in sys.argv:
        rc = pdu_session_and_echo(sys.argv[1], int(flag("--pdu-echo")))
    if rc == 0 and "--edge-echo" in sys.argv:
        rc = pdu_session_and_echo(sys.argv[1], int(flag("--edge-echo")),
                                  dnn="edge", pdu_session_id=2)
    if rc == 0 and "--slice-echo" in sys.argv:
        rc = pdu_session_and_echo(sys.argv[1], int(flag("--slice-echo")),
                                  dnn=flag("--slice-dnn", "internet"), pdu_session_id=3,
                                  snssai=parse_snssai(flag("--slice-nssai", "1")))
    sys.exit(rc)
