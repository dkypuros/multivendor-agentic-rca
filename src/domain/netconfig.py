"""
netconfig: the single source of truth for every owned port, inter-NF base URL, and
identity constant (issue #26). Stdlib only, like everything else in the owned stack.

Everything that used to be a literal in services/**, tests/run_*_spec.py, tools/stackctl.py
and services/ue_sim now resolves through this module. The CLAUDE.md port ledger is generated
from the same tables (tools/gen_port_ledger.py -> docs/operations/port_ledger.txt), so code and ledger
can never drift again.

Resolution order (last one wins):
  1. built-in defaults below (today's exact values — default config changes nothing)
  2. optional config file: TELCO_CONFIG=/path/to/file, simple KEY=VALUE lines
     (# comments and blank lines ignored; keys are the same names as the env vars)
  3. per-key environment variables

Naming convention (the KEY in both the file and the environment):
  TELCO_PORT_<SERVICE>   HTTP/TCP listen port, e.g. TELCO_PORT_NRF=7100
  TELCO_UDP_<NAME>       UDP port, e.g. TELCO_UDP_GTPU_CENTRAL=2152
  TELCO_URL_<SERVICE>    full base URL override, e.g. TELCO_URL_UDM=http://10.0.0.5:7001
                         (when unset the URL is computed as http://<host>:<port>)
  TELCO_HOST             the host every computed URL and socket target points at (127.0.0.1)
  TELCO_PORT_OFFSET      shifts EVERY owned TCP and UDP port by the offset (lab-only device:
                         it moves the well-known GTP-U 2152 / PFCP 8805 / E2 36421 ports too,
                         which no real deployment would do). Explicit TELCO_PORT_*/TELCO_UDP_*
                         overrides are taken literally and NOT shifted.
  TELCO_PLMN             PLMN (mcc+mnc, default 00101)
  TELCO_TEST_K           the shared test key K
  TELCO_IDENTITY_POOL_PREFIX  the BSS identity-pool SUPI prefix
  TELCO_UPF_SELECTION    SMF (sNssai, dnn) -> UPF selection table override, a JSON array
                         (schema + default table: procedures/network_slicing.txt); read
                         through the generic value() accessor below

Pre-existing env names keep working as consumer-scoped aliases (checked only by the one
service that always honoured them, after the canonical TELCO_URL_* name):
  OSS_BMAAS_URL, OSS_OCLOUD_URL          (services/oss/oss.py)
  BSS_OSS_URL                            (services/bss/bss.py)
  AGENT_AIGW_URL, AGENT_OCLOUD_URL, AGENT_BSS_URL  (services/agent/grid_capacity_agent.py)

Helpers: port("nrf"), udp_port("gtpu_central"), url("udm"), host(), plmn(), test_k(),
identity_pool_prefix(). ledger() feeds tools/gen_port_ledger.py.
"""

import os

# ---------------------------------------------------------------- defaults
# (service key, default port, note) — the note is carried into the generated ledger.

TCP_PORTS = {
    # core (7000-7009; NRF sits at 7100 because macOS owns 7000)
    "nrf": 7100,
    "udm": 7001,
    "amf": 7002,
    "smf": 7003,
    "upf": 7004,          # central UPF inspect endpoint (lab instrumentation)
    "edge_upf": 7005,     # edge UPF inspect endpoint
    "pcf": 7006,          # PCF Npcf_SMPolicyControl (policy/PCC rules for the SMF)
    "nssf": 7007,         # standalone NSSF (Nnssf_NSSelection, epic #9 — replaces AMF NSSF-lite)
    "nef": 7008,          # NEF northbound AF-facing exposure surface (Nnef, TS 29.522)
    "chf": 7009,          # CHF Nchf_ConvergedCharging (charging for the SMF)
    "nwdaf": 7016,        # NWDAF read-only analytics (Nnwdaf, TS 29.520)
    "ausf": 7015,         # standalone AUSF (Nausf_UEAuthentication, split from UDM)
    "udr": 7017,          # UDR Nudr unified data repository (TS 29.504) — data layer
    "bsf": 7018,          # BSF Nbsf_Management binding registry (TS 29.521)
    "scp": 7019,          # SCP indirect-SBI proxy (TS 23.501 6.2.19 / 29.500)
    "sepp": 7022,         # SEPP N32 roaming security edge (TS 29.573)
    "udsf": 7023,         # UDSF Nudsf unstructured data storage (TS 29.598)
    "n3iwf": 7024,        # N3IWF non-3GPP access interworking (TS 23.501 4.2.8)
    "nssaaf": 7025,       # NSSAAF slice-specific auth (TS 29.526)
    "eir": 7026,          # 5G-EIR N5g-eir equipment identity check (TS 29.511)
    "capif": 7027,       # CAPIF Common API Framework (TS 29.222) — the API gateway fronting NEF
    "lmf": 7028,         # LMF Location Management (Nlmf, TS 29.572)
    "gmlc": 7029,        # GMLC location gateway (Ngmlc, TS 23.273)
    "pcscf": 7033,       # IMS P-CSCF (SIP proxy, entry to IMS)
    "icscf": 7034,       # IMS I-CSCF (interrogating CSCF)
    "scscf": 7035,       # IMS S-CSCF (serving CSCF, registrar)
    "ims_hss": 7036,     # IMS-HSS (Cx/Sh subscriber data for IMS)
    "mrf": 7037,         # MRF media resource function (MRFC/MRFP)
    "tsctsf": 7038,      # TSCTSF time-sensitive comms / TSN (TS 29.565)
    # split RAN (7010-7019)
    "odu": 7010,          # O-DU RRC/F1AP (also the retired monolithic gNB's RRC port)
    "ocucp": 7012,        # F1-C + debug
    "ocuup": 7013,        # E1 + debug
    # RIC/SMO (7020-7029)
    "nearrt_ric": 7020,   # A1-P + debug
    "smo": 7021,
    "r1": 7022,           # R1 (rApp <-> non-RT RIC framework), O-RAN WG2 R1GAP
    "o1_netconf": 7023,   # O1 NETCONF/YANG. Real O1 is NETCONF-over-SSH on 830; this lab
                          # speaks correct RFC 6242 framing over TCP -- a labelled transport
                          # simplification, the RPC layer and the YANG shapes are real.
    # edge plane (7030-7039)
    "ocloud": 7030,       # O2 IMS/DMS + MEC service mgmt
    "edge_app": 7031,     # EAS HTTP status
    # OSS plane (7040-7049 reserved)
    "oss": 7040,
    "osac_closure": 7041,   # AI-grid closure bridge (spoke Available -> OSAC status + TMF639)
    "eventstore": 7042,  # TMF688 subscriber: durable audit record of every published event
    "evidence": 7043,    # end-to-end evidence page: claim vs independent confirmation, live
    "mme": 7043,         # 4G EPC MME (S1-MME NAS/EMM/ESM, S6a to HSS)
    "sgw": 7044,         # 4G EPC Serving Gateway (S5/S8, S1-U GTP-U)
    "pgw": 7045,         # 4G EPC PDN Gateway (SGi, S5/S8, UE IP anchor)
    "hss4g": 7046,       # 4G EPC HSS (S6a subscriber + EPS-AKA auth vectors)
    # BSS plane (7050-7059 reserved)
    "bss": 7050,
    # billing plane (7060-7069 reserved)
    "bss_billing": 7060,
    # agent plane (7070-7079 reserved)
    "ran_heal_agent": 7070,
    "grid_capacity_agent": 7071,
    # SMO perception bridge (services/smo/bridge.py): RAN events -> VES -> the R1/DME
    # store. It belongs to the RIC/SMO block by function, but 7020-7029 is fully taken,
    # so it borrows the agent plane's 7071. grid_capacity_agent is therefore the ONE
    # service it must never be run alongside — both are optional add-ons, neither is in
    # the base stack profile, so they never collide in practice. Override TELCO_PORT_SMO_BRIDGE
    # if a profile ever needs both.
    "smo_bridge": 7071,
    # frontend plane (7080-7089 reserved) — src-frontend, issue #41 (Node runtime, exempt layer)
    "frontend_bss": 7080,      # Next.js storefront, the single front door (multi-zone root)
    "frontend_oss": 7081,      # Next.js NOC console zone (basePath /oss)
    "frontend_gateway": 7082,  # NestJS typed BFF (/api/*)
    "frontend_cockpit": 7083,  # stdlib TMF cockpit console (services/cockpit/server.py),
                               # serves the single-file console + proxies the real gateway
                               # server-side (no CORS needed); additive, fully independent
                               # of the Next.js zones above
    # grid plane (issue #17): the owned Rafay-style five-verb control plane. gridctl
    # SUPERSEDES bmaas_stub for the DEPLOYED stack — same default port 8700, contract
    # superset — so OSS/BSS clients swap stub -> owned service with zero changes; specs may
    # still run the lightweight tests/helpers/bmaas_stub.py fixture (never both at once).
    "gridctl": 8700,
    # slice-management plane (7090-7099 reserved) — epic #11
    "nsmf": 7091,              # NSMF-style slice management (TS 28.531-flavored provisioning)
    # spec fixtures (tests/helpers — aigrid-sim contract stubs, not owned services)
    "bmaas_stub": 8700,
    "aigateway_stub": 8710,
    "dnsreg_stub": 8720,   # toy DNS-ish registry, the connector-extensibility fixture (issue #33)
    "osac_stub": 8730,     # OSAC fulfillment-service contract fixture (ai-grid order spec)
}

UDP_PORTS = {
    "uu": 7011,            # O-DU Uu user plane (shared with the retired monolithic gNB)
    "fronthaul": 7014,     # O-RU -> O-DU fh-heartbeat
    "gtpu_central": 2152,  # central UPF GTP-U (well-known TS 29.281 port)
    "gtpu_gnb": 2154,      # retired monolithic gNB N3 GTP-U
    "gtpu_cuup": 2155,     # O-CU-UP F1-U/N3 GTP-U
    "f1u": 2156,           # O-DU F1-U GTP-U
    "gtpu_edge": 2157,     # edge UPF GTP-U
    "eas_dn": 7032,        # edge app local DN echo socket
    "n4_central": 8805,    # central UPF PFCP (well-known port)
    "n4_edge": 8806,       # edge UPF PFCP
    "e2": 36421,           # O-DU E2 (well-known E2AP port)
    "e2_ind": 36422,       # near-RT RIC E2 indications
}

IDENTITY = {
    "plmn": "00101",
    # The serving Tracking Area Code. One TAC is the whole lab by default; a LADN service
    # area is a SET of these, so a multi-zone demo overrides it per gNB to move the UE
    # between areas (TELCO_TAC, or tac= on the registration from the RAN).
    "tac": "000001",
    "test_k": "0f0e0d0c0b0a09080706050403020100",
    "identity_pool_prefix": "imsi-0010100000007",
}

DEFAULT_HOST = "127.0.0.1"

# ------------------------------------------------------------- config file

_FILE_VALUES = {}


def _load_file():
    path = os.environ.get("TELCO_CONFIG")
    if not path:
        return {}
    values = {}
    try:
        text = open(path).read()
    except OSError as exc:
        raise RuntimeError(f"netconfig: TELCO_CONFIG={path} is not readable: {exc}")
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError(f"netconfig: {path}:{lineno}: expected KEY=VALUE, got {line!r}")
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


_FILE_VALUES = _load_file()


def _raw(key, aliases=()):
    """(value, source) per the resolution order; (None, 'default') if nothing set."""
    if key in os.environ:
        return os.environ[key], "env"
    for alias in aliases:
        if alias in os.environ:
            return os.environ[alias], f"env({alias})"
    if key in _FILE_VALUES:
        return _FILE_VALUES[key], "file"
    return None, "default"


# ----------------------------------------------------------------- lookups


def offset():
    raw, _ = _raw("TELCO_PORT_OFFSET")
    return int(raw) if raw is not None else 0


def host():
    raw, _ = _raw("TELCO_HOST")
    return raw if raw is not None else DEFAULT_HOST


def bind_host():
    """Address HTTP servers bind (TELCO_BIND). Defaults to host(), i.e. loopback.

    Deployment knob (issue #30 seed): TELCO_BIND=0.0.0.0 exposes every HTTP surface on
    all interfaces (e.g. the tailnet) while inter-NF URLs keep targeting host() — the
    control plane keeps talking over loopback, only the listening sockets widen. The
    user-plane UDP sockets (adapters/udp_json.py) deliberately stay on loopback."""
    raw, _ = _raw("TELCO_BIND")
    return raw if raw is not None else host()


def _port(table, prefix, name):
    if name not in table:
        raise KeyError(f"netconfig: unknown service key {name!r}")
    raw, source = _raw(prefix + name.upper())
    if raw is not None:
        return int(raw), source
    off = offset()
    return table[name] + off, ("default+offset" if off else "default")


def port(name):
    """Effective HTTP/TCP listen port for a service key, e.g. port('nrf')."""
    return _port(TCP_PORTS, "TELCO_PORT_", name)[0]


def udp_port(name):
    """Effective UDP port for a named socket, e.g. udp_port('gtpu_central')."""
    return _port(UDP_PORTS, "TELCO_UDP_", name)[0]


def url(name, *aliases):
    """Effective base URL (no trailing slash) for a service key, e.g. url('udm').
    Extra args are consumer-scoped legacy env aliases (see module docstring)."""
    raw, _ = _raw("TELCO_URL_" + name.upper(), aliases)
    if raw is not None:
        return raw.rstrip("/")
    return f"http://{host()}:{port(name)}"


def _identity(name, env_key):
    raw, _ = _raw(env_key)
    return raw if raw is not None else IDENTITY[name]


def plmn():
    return _identity("plmn", "TELCO_PLMN")


def tac():
    return _identity("tac", "TELCO_TAC")


def test_k():
    return _identity("test_k", "TELCO_TEST_K")


def identity_pool_prefix():
    return _identity("identity_pool_prefix", "TELCO_IDENTITY_POOL_PREFIX")


def value(key, default=None):
    """Free-form declarative config lookup with the same file <- env precedence as every
    other key (e.g. TELCO_UPF_SELECTION, the SMF's slice/DNN -> UPF selection table)."""
    raw, _ = _raw(key)
    return raw if raw is not None else default


# ------------------------------------------------------------------ ledger


def ledger():
    """Every owned port with its effective value and source, for gen_port_ledger.py.
    Yields dicts: {name, proto, port, default, source}."""
    rows = []
    for name in TCP_PORTS:
        p, source = _port(TCP_PORTS, "TELCO_PORT_", name)
        rows.append({"name": name, "proto": "tcp", "port": p,
                     "default": TCP_PORTS[name], "source": source})
    for name in UDP_PORTS:
        p, source = _port(UDP_PORTS, "TELCO_UDP_", name)
        rows.append({"name": name, "proto": "udp", "port": p,
                     "default": UDP_PORTS[name], "source": source})
    return rows
