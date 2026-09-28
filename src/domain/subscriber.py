"""
Canonical subscriber model: the access-and-mobility slice of 5G subscription data.

Spec anchors: SUPI format TS 23.003 section 2.2A; subscription data TS 23.501 section 5.15 and
TS 29.503 (Nudm_SDM am-data). The permanent key k exists only to feed the clearly-labeled FAKE
5G-AKA in services/core/udm/udm.py (real algorithm: TS 33.501 section 6.1.3.2, out of scope).

Network slicing wave 1 (epic #11): the S-NSSAI list is the SUBSCRIBED NSSAI (TS 23.501
section 5.15.3), named subscribedNssai; the AMF's NSSF-lite intersects it with the UE's
requested NSSAI into the allowed NSSAI. from_record() accepts the legacy key "nssai" so
pre-slicing seed files, state-store rows and Nudr provisioning payloads keep loading.
"""

import json
from dataclasses import dataclass

from domain import snssai as snssai_mod


@dataclass
class Subscriber:
    supi: str              # "imsi-<mcc><mnc><msin>"
    k: str                 # permanent key, hex string (fake-crypto input only)
    plmn: str              # e.g. "00101"
    subscribedNssai: list  # subscribed S-NSSAIs (TS 23.501 5.15.3), e.g. [{"sst": 1}]
    status: str            # "ACTIVE" | "SUSPENDED"

    @classmethod
    def from_record(cls, record):
        """Build from a dict keyed subscribedNssai (current) OR nssai (legacy). The list is
        normalized entry by entry (raises ValueError on a malformed S-NSSAI)."""
        r = dict(record)
        nssai = r.pop("subscribedNssai", None) or r.pop("nssai", None) or [{"sst": 1}]
        r.pop("nssai", None)
        r["subscribedNssai"] = [snssai_mod.normalize(s) for s in nssai]
        return cls(**r)


def load_subscribers(path):
    with open(path) as f:
        return {s["supi"]: Subscriber.from_record(s) for s in json.load(f)}
