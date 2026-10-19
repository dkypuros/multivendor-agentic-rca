# note: this code already exists locally

`gh repo clone dkypuros/multivendor-agentic-rca` (cloned here, 2026-10-19) is a standalone
extraction of code that already lives in:

  ../86_intel_megalab_gitea/telco-lab/

Per this repo's own README ("Provenance" section): extracted from the `telco-lab` monorepo
at Gitea commit `a1bb39e`, keeping only the 69 files this RCA use case runs (out of ~1,400).

Matching source paths in telco-lab:
- services/ran/sandbox_controller.py
- services/ue_sim/ue_sim.py
- services/smo/ptp_bridge.py
- services/orchestrator/nep_orchestrator.py
- extensions/sheldon/agentic/  (gateway.py, rca_agent.py, ran_tools.py, ptp_tools.py,
  nic_tools.py, ocloud_tools.py, mcp_server.py, mcp_ran.py, mcp_intel.py, mcp_redhat.py,
  trust.py, guardrail.py, loop.py)

The same core files (sandbox_controller.py, nep_orchestrator.py, rca_agent.py) also exist in
two sibling repos in 86_intel_megalab_gitea: redhat-telco-to-techco and redhat-telco-use-cases.

telco-lab's origin remote is gitea-gitea.apps.oberon.fm2aihpcsed.com/fedora/telco-lab.git and
is still intact — this extraction did not move or rename anything out of 86.

Cross-link: telco-lab/CLAUDE.md's CROSS-LINKS section now points back here too.
