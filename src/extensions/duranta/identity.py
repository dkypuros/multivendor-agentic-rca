"""
Canonical Duranta lab identity — the source-of-truth values the real OAI stack
was deployed with (../duranta-e2e/5g-lab/identity.yaml). Federation writes must
match these or the real AMF/AUSF/UDM reject the subscriber.

Defaults are the committed lab identity; every value is env-overridable so this
adapter follows the lab if it is redeployed with different numbers. A future
refinement can parse identity.yaml directly (DURANTA_IDENTITY path); the values
below are kept in sync with it by hand today (labelled simplification).
"""

import os

# PLMN / slice / DNN (identity.yaml)
PLMN = os.environ.get("DURANTA_PLMN", "00101")          # mcc 001 + mnc 01
SST = int(os.environ.get("DURANTA_SST", "1"))
SD = os.environ.get("DURANTA_SD", "FFFFFF")
DNN = os.environ.get("DURANTA_DNN", "oai")
UE_SUBNET_PREFIX = os.environ.get("DURANTA_UE_SUBNET_PREFIX", "12.1.1")  # /24

# 5G-AKA credentials shared across the lab's seeded subscribers (MILENAGE test keys)
KEY = os.environ.get("DURANTA_KEY", "fec86ba6eb707ed08905757b1bb44b8f")
OPC = os.environ.get("DURANTA_OPC", "C42449363BBAD02B66D16BC975D77CC1")
AMF = os.environ.get("DURANTA_AMF", "8000")
SQN = os.environ.get("DURANTA_SQN", "000000000020")

# The lab's own subscriber pool is 001010000000100..104 and the running NR-UE is
# pinned to ...100. Federation-provisioned subscribers use a disjoint range so a
# owned stack order NEVER disturbs the working UE.
FEDERATION_IMSI_BASE = os.environ.get("DURANTA_FED_IMSI_BASE", "001010000000200")


def default_static_ip(imsi):
    """A static UE IPv4 in the lab's DNN subnet, keyed off the IMSI's last octet
    so federation subscribers land at 12.1.1.2xx, clear of the lab's 12.1.1.10x."""
    last = int(imsi[-2:]) if imsi[-2:].isdigit() else 0
    return f"{UE_SUBNET_PREFIX}.{200 + (last % 55)}"


def subscriber_defaults(imsi):
    return {
        "imsi": imsi,
        "key": KEY,
        "opc": OPC,
        "amf": AMF,
        "sqn": SQN,
        "plmn": PLMN,
        "sst": SST,
        "sd": SD,
        "dnn": DNN,
        "static_ip": default_static_ip(imsi),
    }
