"""
Subscriber provisioning against the REAL OAI UDR (`oai_db`).

Authors the two rows a UE needs to register (5G-AKA) and open a PDU session,
matching the OAI schema exactly:
  - AuthenticationSubscription       (MILENAGE key/opc, 5G_AKA)   -> registration
  - SessionManagementSubscriptionData (S-NSSAI + DNN + static IP)  -> PDU session

provision() is idempotent (delete-then-insert per ueid). deprovision() removes
both rows, so a federated subscriber is fully reversible — a owned stack order that
creates one can be un-made with no trace left in the real core.

The credentials/DNN/S-NSSAI come from identity.py (the lab's deployed identity).
No OAI code is copied; only its documented schema is written.
"""

import json

from . import cluster, identity


def _bare(imsi):
    """The real OAI oai_db keys subscribers by BARE SUPI digits (e.g. 001010000000102),
    matching its own DB-init and fitting SessionManagementSubscriptionData.ueid varchar(15).
    Strip any imsi- NAI prefix so the row lands AND the AMF/AUSF can resolve the subscriber
    the UE actually registers as (2026-07-20: real-provisioning weld, Phase 1)."""
    return imsi[5:] if imsi.startswith("imsi-") else imsi



def _auth_row_sql(sub):
    seq = json.dumps({"sqn": sub["sqn"], "sqnScheme": "NON_TIME_BASED",
                      "lastIndexes": {"ausf": 0}})
    return (
        "DELETE FROM AuthenticationSubscription WHERE ueid='{imsi}';"
        "INSERT INTO AuthenticationSubscription "
        "(ueid,authenticationMethod,encPermanentKey,protectionParameterId,"
        "sequenceNumber,authenticationManagementField,algorithmId,encOpcKey,"
        "encTopcKey,vectorGenerationInHss,n5gcAuthMethod,rgAuthenticationInd,supi) "
        "VALUES ('{imsi}','5G_AKA','{key}','{key}','{seq}','{amf}','milenage',"
        "'{opc}',NULL,NULL,NULL,NULL,'{imsi}');"
    ).format(imsi=sub["imsi"], key=sub["key"], seq=seq, amf=sub["amf"], opc=sub["opc"])


def _smd_row_sql(sub):
    nssai = json.dumps({"sst": sub["sst"], "sd": sub["sd"]})
    dnn_cfg = json.dumps({sub["dnn"]: {
        "pduSessionTypes": {"defaultSessionType": "IPV4"},
        "sscModes": {"defaultSscMode": "SSC_MODE_1"},
        "5gQosProfile": {"5qi": 1, "arp": {"priorityLevel": 15,
                         "preemptCap": "NOT_PREEMPT", "preemptVuln": "PREEMPTABLE"},
                         "priorityLevel": 1},
        "sessionAmbr": {"uplink": "1000Mbps", "downlink": "1000Mbps"},
        "staticIpAddress": [{"ipv4Addr": sub["static_ip"]}],
    }})
    return (
        "DELETE FROM SessionManagementSubscriptionData WHERE ueid='{imsi}';"
        "INSERT INTO SessionManagementSubscriptionData "
        "(ueid,servingPlmnid,singleNssai,dnnConfigurations) "
        "VALUES ('{imsi}','{plmn}','{nssai}','{dnn_cfg}');"
    ).format(imsi=sub["imsi"], plmn=sub["plmn"], nssai=nssai, dnn_cfg=dnn_cfg)


def provision(imsi, **overrides):
    """Write a real subscriber into oai_db. Returns the provisioned record."""
    imsi = _bare(imsi)
    sub = identity.subscriber_defaults(imsi)
    sub.update({k: v for k, v in overrides.items() if v is not None})
    cluster.sql(_auth_row_sql(sub) + _smd_row_sql(sub))
    return sub


def get(imsi):
    """Return {imsi, key, opc, static_ip} if the subscriber exists in the real
    UDR, else None. Reads both tables to prove registration + session rows landed."""
    imsi = _bare(imsi)
    auth = cluster.sql(
        f"SELECT ueid,encPermanentKey,encOpcKey FROM AuthenticationSubscription "
        f"WHERE ueid='{imsi}';").strip()
    if not auth:
        return None
    ueid, key, opc = (auth.split("\t") + ["", ""])[:3]
    smd = cluster.sql(
        f"SELECT dnnConfigurations FROM SessionManagementSubscriptionData "
        f"WHERE ueid='{imsi}';").strip()
    static_ip = None
    if smd:
        try:
            cfg = json.loads(smd)
            static_ip = next(iter(cfg.values()))["staticIpAddress"][0]["ipv4Addr"]
        except (ValueError, KeyError, StopIteration, IndexError):
            static_ip = None
    return {"imsi": ueid, "key": key, "opc": opc, "static_ip": static_ip,
            "session_data": bool(smd)}


def deprovision(imsi):
    """Remove both rows for the subscriber. Idempotent; returns True."""
    imsi = _bare(imsi)
    cluster.sql(
        f"DELETE FROM AuthenticationSubscription WHERE ueid='{imsi}';"
        f"DELETE FROM SessionManagementSubscriptionData WHERE ueid='{imsi}';")
    return True
