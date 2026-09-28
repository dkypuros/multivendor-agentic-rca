"""
extensions/duranta — the Duranta (OAI 5G SA) federation adapter.

CHARTER ROLE: FEDERATE, not author. Duranta is real, bit-level radio truth
(real ASN.1 PER, real NAS, real MILENAGE, real PHY via RFsim) running on the lab
Kubernetes cluster. The owned stack does NOT rebuild it — it CONNECTS to it over its
real interfaces. This package is the only place that knows how to reach the real
cluster; the owned core (services/oss/connectors/duranta_subscriber.py) depends
on it lazily and degrades to "unavailable" when the cluster is absent, so the
stdlib specs and the golden scenario never require real infrastructure.

What is modeled vs real (honesty header, per the aigrid-sim adapter pattern):
  - REAL: the OAI 5G core (NRF/AMF/SMF/UPF/UDM/UDR/AUSF), the MySQL `oai_db`
    subscriber database, the gNB (RFsim) and NR-UE softmodem — all running in the
    `duranta` namespace on node student-gpu-worker.
  - THIS ADAPTER: authors the SQL that provisions/removes a subscriber in that
    real UDR, and runs it via the lab's kubectl helper into the live MySQL pod.
    No OAI code is copied; only its documented on-disk schema is written to.

Reference (read-only, never vendored): the OAI subscriber schema at
../duranta-e2e/5g-lab/charts/.../mysql/initialization/oai_db-basic.sql and the
canonical identity at ../duranta-e2e/5g-lab/identity.yaml.
"""

from . import cluster, identity, udr  # noqa: F401
