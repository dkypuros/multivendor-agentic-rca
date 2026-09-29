"""Agentic RCA surface used by multivendor-agentic-rca.

  gateway.py      MCP gateway: CAPIF bearer token -> tool scope -> route to a plane server
  mcp_http.py     serves one plane's MCP server over HTTP (--server ocloud|ran|intel|redhat)
  mcp_core.py     shared JSON-RPC/MCP handler
  mcp_server.py   O-Cloud plane (ocloud_tools.py: OCM/ACM ManagedClusters via kubectl)
  mcp_ran.py      RAN plane (ran_tools.py: O-DU O1 status + alarms)
  mcp_redhat.py   PTP plane (ptp_tools.py: ptp-bridge)
  mcp_intel.py    NIC plane (nic_tools.py: EMULATED Intel E810 counters)
  guardrail.py, trust.py   upstream action-governance layer; its policy files (harness/sheldon/)
                           are not shipped, so the O-Cloud *action* tools refuse to run here.
"""
