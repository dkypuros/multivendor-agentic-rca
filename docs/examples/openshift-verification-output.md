# Example output: OpenShift verification steps

Real output of the "is that real?" commands, run against the sandbox on OpenShift on 29 Sep 2026, a few minutes after the [example heal/re-check run](heal-recheck-run.md). Back to the [demo walkthrough](../demo-walkthrough.md).

## The pod the Start button created

```console
$ oc project multivendor-rca
$ oc get pods -l app=ran-slice -o wide
NAME                        READY   STATUS    RESTARTS   AGE   IP             NODE     NOMINATED NODE   READINESS GATES
ran-slice-f9f4d6cf6-k6sfk   1/1     Running   0          25m   10.128.1.135   worker-0 <none>           <none>
```

## The network functions' own log lines

Two attaches (11:39 and 11:53), each followed by an Inject (O-DU `LOCKED`) and a Heal (`UNLOCKED`). The `corr` id ties the AMF and SMF lines of one attach together.

```console
$ oc logs deploy/ran-slice | grep -E 'registration_accepted|session_created|o1_config_applied'
{"ts": "2026-09-29T11:39:57Z", "nf": "amf", "level": "info", "event": "registration_accepted", "corr": "c-a76142ad7f97", "supi": "imsi-001010000000001", "state": "REGISTERED", "allowedNssai": ["1"]}
{"ts": "2026-09-29T11:39:57Z", "nf": "smf", "level": "info", "event": "session_created", "corr": "c-a76142ad7f97", "supi": "imsi-001010000000001", "seid": "imsi-001010000000001-1", "dnn": "internet", "ueIp": "10.45.0.2", "snssai": "1", "upf": "central", "policy": "internet-default", "fiveqi": 9, "chargingRef": "cdr-dcafcd2136cf"}
{"ts": "2026-09-29T11:42:07Z", "nf": "odu", "level": "info", "event": "o1_config_applied", "administrativeState": "LOCKED"}
{"ts": "2026-09-29T11:44:35Z", "nf": "odu", "level": "info", "event": "o1_config_applied", "administrativeState": "UNLOCKED"}
{"ts": "2026-09-29T11:53:04Z", "nf": "amf", "level": "info", "event": "registration_accepted", "corr": "c-b6232fc581ce", "supi": "imsi-001010000000001", "state": "REGISTERED", "allowedNssai": ["1"]}
{"ts": "2026-09-29T11:53:04Z", "nf": "smf", "level": "info", "event": "session_created", "corr": "c-b6232fc581ce", "supi": "imsi-001010000000001", "seid": "imsi-001010000000001-1", "dnn": "internet", "ueIp": "10.45.0.3", "snssai": "1", "upf": "central", "policy": "internet-default", "fiveqi": 9, "chargingRef": "cdr-6b78ca07e5f1"}
{"ts": "2026-09-29T11:53:58Z", "nf": "odu", "level": "info", "event": "o1_config_applied", "administrativeState": "LOCKED"}
{"ts": "2026-09-29T11:54:21Z", "nf": "odu", "level": "info", "event": "o1_config_applied", "administrativeState": "UNLOCKED"}
```

## The latest RCA's TMF688 event

This is the post-heal RCA, so the verdict is `NO ACTION`. `—` is the JSON escape for the em dash in the decision text.

```console
$ oc exec deploy/nep-orchestrator -- python3 -c "import urllib.request,json; \
  print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:7095/nep/audit/latest'))['tmf688Event'], indent=1))"
{
 "eventId": "evt-5b755c437e3c",
 "eventTime": "2026-09-29T11:54:50.885949+00:00",
 "eventType": "RcaConcludedEvent",
 "event": {
  "traceId": "tr-oran-tmf-4815649e593e",
  "mlflowRunId": "run-9796fe5b",
  "trigger": "PTP sync fault (cell unavailable, FREERUN<->LOCKED)",
  "invoker": "invoker-2073854b9b8b",
  "capifScope": "3gpp#mcp-aef:mcp-tools",
  "faultClass": null,
  "signal": null,
  "corroboration": "0/3",
  "decision": "NO ACTION — no fault signals",
  "planes": [],
  "evidence": [
   {
    "plane": "cluster",
    "vendor": "O-Cloud/Red Hat",
    "tool": "ocloud_cluster_health",
    "answered": false,
    "emulated": false,
    "signals": [],
    "evidence": "unavailable: ocloud_unavailable kubectl failed: [Errno 2] No such file or directory: 'kubectl'"
   },
   {
    "plane": "ran",
    "vendor": "RAN (O-DU O1)",
    "tool": "ran_gnb_status",
    "answered": true,
    "emulated": false,
    "signals": [],
    "evidence": "cell ACTIVE, RU CONNECTED, admin UNLOCKED, no alarms"
   },
   {
    "plane": "platform",
    "vendor": "PTP/Red Hat",
    "tool": "ptp_operator_status",
    "answered": true,
    "emulated": false,
    "signals": [],
    "evidence": "ptp4l offset 0 ns (limit 100000), DU port SLAVE"
   },
   {
    "plane": "hardware",
    "vendor": "NIC/Intel (emulated)",
    "tool": "nic_timestamp_counters",
    "answered": true,
    "emulated": true,
    "signals": [],
    "evidence": "EMULATED ethtool -S: tx_hwtstamp_timeouts=0"
   }
  ],
  "llmNarrative": "[template - LLM unavailable] Diagnosis none (signal none), corroboration 0/3, decision: NO ACTION — no fault signals. Evidence - cluster: unavailable: ocloud_unavailable kubectl failed: [Errno 2] No such file or directory: 'kubectl'; ran: cell ACTIVE, RU CONNECTED, admin UNLOCKED, no alarms; platform: ptp4l offset 0 ns (limit 100000), DU port SLAVE; hardware: EMULATED ethtool -S: tx_hwtstamp_timeouts=0."
 }
}
```

The `cluster` plane is unavailable because this deployment has no ACM; the walkthrough expects that.

## Replica count

```console
$ oc get deploy ran-slice -o jsonpath='{.spec.replicas}'
1
```

`1` while the slice runs, `0` after Stop.
