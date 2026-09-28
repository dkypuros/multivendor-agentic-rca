"""services.smo -- the RAN-domain manager slot (SMO stand-in), centralized.

Pulled from the O-RAN speaking-session repo (Telco_Systems_Integration_Lab)
into the canonical system on 2026-08-01. Layout:

  oam/        O1 / VES / PM / TEIV surfaces (O-RAN WG10-shaped mocks)
  telemetry/  the EIAP-data-layer mock: store, generator, summarizer, R1 DME
  perception/ the rApp-facing R1/DME query facade (layered over telemetry)
  harness/    the agent harness: zero-trust boundary, intent translator, MCP server

This fills the dashed RAN DOMAIN box on deck page 6: perception up through
R1/DME, actuation intended to land as TMF641 orders into services/oss.
"""
