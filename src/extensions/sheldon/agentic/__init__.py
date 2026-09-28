"""Agentic closed loop over the real Sheldon O-Cloud.

The O-Cloud itself (deploy/sheldon/, extensions/sheldon/o2ims_reconciler.py) already
provisions bare metal from an O-RAN O2-IMS ProvisioningRequest. This package adds the
GOVERNANCE around that capability — the part that decides whether an action may run at
all, and leaves an auditable account of why it did.

  ocloud_tools.py  the typed tool surface: the ONLY code that reads or writes the O-Cloud
  guardrail.py     the LLM-free layer that says no (allowlist, sandbox gate, blast caps)
  trust.py         the licence ledger: promotion is earned, demotion is automatic
  mcp_server.py    exposes the tool surface over MCP so a vendor's agent can testify
  loop.py          the closed loop: testimony -> route -> rehearse -> guard -> act -> audit

Declarative contracts live in harness/sheldon/ (taxonomy, guardrails, trust registry,
JSON schemas) so the behaviour is readable without reading the code.
"""
