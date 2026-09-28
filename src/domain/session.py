"""
Canonical PDU session model.

Spec anchors: PDU session concept TS 23.501 section 5.6; establishment procedure TS 23.502
section 4.3.2.2; the SMF-side record here mirrors the SM context of TS 29.502 section 6.1,
simplified. TEIDs identify tunnel directions per GTP-U (TS 29.281); the UE IP comes from the
SMF-managed pool (10.45.0.0/16 in this stack).
"""

from dataclasses import asdict, dataclass


@dataclass
class PduSession:
    seid: str        # session endpoint id used on N4, "<supi>-<pduSessionId>"
    supi: str
    pduSessionId: int
    ueIp: str
    dnn: str         # data network name (TS 23.003 section 9A)
    sNssai: dict     # selected slice, e.g. {"sst": 1}
    ulTeid: int      # uplink tunnel id, gNB -> UPF
    dlTeid: int      # downlink tunnel id, UPF -> gNB
    gnbGtpu: dict    # {"ip", "port"}: the N3 endpoint the UPF sends downlink to
    state: str       # "ACTIVE"
    upf: str = "central"   # which PSA anchors this session (slicing wave 1: the SMF's
                           # (sNssai, dnn) selection table decides — see smf.py)
    # PCF-authorized policy (Npcf_SMPolicyControl, TS 29.512) applied at establishment.
    # ALL default None so a session created without a PCF (the legacy path) is byte-identical
    # to before — the SMF only fills these when a PCF authorized the session (see smf.py).
    smPolicyId: str = None       # the PCF policy association id (for later GET/DELETE)
    authorizedQos: dict = None   # {"5qi": int, "arp": {...}} default QoS the PCF authorized
    sessionAmbr: dict = None     # {"uplink", "downlink"} authorized session-AMBR
    gateStatus: str = None       # "OPEN"/"CLOSED" default-flow gate the PCF decided
    policyLabel: str = None      # which policy fired (for logs/UI); None = legacy fallback
    # CHF converged-charging resource (Nchf_ConvergedCharging, TS 32.291) opened at
    # establishment. BOTH default None so a session created without a CHF (the legacy path) is
    # byte-identical to before — the SMF only fills these when a CHF granted a quota (see smf.py).
    chargingRef: str = None      # the CHF charging-data resource id (for update/release)
    grantedQuota: dict = None    # the initial GSU the CHF granted {totalVolume, time, ...}

    def as_dict(self):
        return asdict(self)
