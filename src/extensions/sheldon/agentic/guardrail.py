"""The layer that says no. LLM-free by construction.

Nothing in this module calls a model, samples, or scores. A policy layer that samples is
not a policy layer; a rule with a temperature is an opinion. Every decision here is a
lookup or a comparison against harness/sheldon/guardrails.yaml, so the same proposal
always gets the same verdict, and the verdict can be re-derived by hand from the YAML.

Evaluation order matters, and it is deliberate:

  1. CRISIS MODE      if a human flipped it, everything is forced to dry-run and approval
  2. SANDBOX GATE     no verdict, or a failing verdict -> REFUSE. Checked BEFORE the
                      allowlist so that a human is never shown an unrehearsed proposal
  3. ALLOWLIST        the action must be named in guardrails.yaml. Not a denylist: we do
                      not attempt to enumerate everything an agent might invent
  4. CLASS POLICY     the action must be permitted for THIS fault class
  5. PROTECTED        the target must not be in the fenced core (never auto-remediated)
  6. BLAST CAPS       absolute ceilings, enforced regardless of who authorized the action
  7. BUSINESS AUTH    change freeze, and how many LIVE services this would touch. Blast
                      radius says what could break; this says whether we are permitted to
                      run it right now. Only the service plane knows either.
  8. LICENCE          the class's trust state decides human-signed vs policy-auto

The verdict is data, not prose: `allowed`, `mode`, and `findings` (machine-readable
reason codes). loop.py copies it verbatim into the audit event's `authorized` link.
"""

import os

import yaml

HARNESS_DIR = os.environ.get(
    "SHELDON_HARNESS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "harness", "sheldon"))
GUARDRAILS = os.path.abspath(os.path.join(HARNESS_DIR, "guardrails.yaml"))

# Ordered weakest -> strongest. A class may auto-apply only at supervised-auto or above.
STATES = ["observed", "advised", "rehearsed", "supervised-auto", "governed-auto"]
AUTO_STATES = {"supervised-auto", "governed-auto"}


def load(path=GUARDRAILS):
    with open(path) as fh:
        return yaml.safe_load(fh)


def evaluate(proposal, trust_state, policy=None):
    """Judge one proposal. Returns a verdict dict; never raises on a bad proposal.

    proposal     harness/sheldon/schemas/proposal.schema.json shape
    trust_state  the licence the fault class currently holds (from trust.py)
    """
    policy = policy or load()
    g = policy.get("global", {})
    findings = []

    fault_class = proposal.get("fault_class", "")
    action = proposal.get("action", "")
    target = proposal.get("target", "")
    cls = (policy.get("classes", {}) or {}).get(fault_class)

    # 1. crisis mode ---------------------------------------------------------------
    crisis = (g.get("crisis_mode") or {})
    in_crisis = bool(crisis.get("enabled"))
    if in_crisis:
        findings.append("crisis_mode_active")

    # 2. sandbox gate — checked first so no unrehearsed proposal reaches a human ----
    needs_rehearsal = bool(g.get("require_sandbox_verdict", True))
    if cls is not None:
        needs_rehearsal = bool(cls.get("require_rehearsal", needs_rehearsal))
    verdict = proposal.get("sandbox_verdict")
    if needs_rehearsal:
        if not verdict:
            findings.append("no_sandbox_verdict")
            return _refuse(findings)
        if not verdict.get("apply_allowed"):
            findings.append("sandbox_verdict_failed")
            return _refuse(findings)
        if float(verdict.get("twin_divergence", 0.0) or 0.0) != 0.0:
            findings.append("twin_divergence_nonzero")
            return _refuse(findings)

    # 3. allowlist -----------------------------------------------------------------
    allowed_actions = {a["action"]: a for a in policy.get("allowlist", [])}
    if action not in allowed_actions:
        findings.append("not_in_allowlist")
        return _refuse(findings)

    # 4. per-class policy ----------------------------------------------------------
    if cls is None:
        findings.append("no_policy_for_class")
        return _refuse(findings)
    if action not in (cls.get("permitted_actions") or []):
        findings.append("action_not_permitted_for_class")
        return _refuse(findings)

    # 5. protected resources — the fenced core -------------------------------------
    for prot in g.get("protected_resources", []) or []:
        if target == f"{prot.get('kind')}/{prot.get('name')}":
            findings.append("target_is_protected")
            return _refuse(findings)

    # 6. blast caps ----------------------------------------------------------------
    touched = int(proposal.get("parameters", {}).get("nodes_touched", 1))
    cap = min(int(g.get("max_nodes_touched_per_action", 1)),
              int(cls.get("max_nodes_touched", 1)))
    if touched > cap:
        findings.append("blast_radius_exceeded")
        return _refuse(findings)

    # 7. business authorization ----------------------------------------------------
    # An infrastructure-only guardrail would happily power-cycle a node carrying a live
    # customer service during a change freeze. It has no way to know. The service plane
    # does, so its assessment is a first-class input here.
    biz = (g.get("business_authorization") or {})
    impact = (proposal.get("parameters", {}) or {}).get("service_impact") or {}
    needs_biz_approval = False
    if biz.get("change_freeze"):
        findings.append("change_freeze_active")
        needs_biz_approval = True
    if biz.get("require_impact_assessment", True) and not impact.get("known", False):
        # Unknown impact is treated AS impact. Silence from the service plane is not
        # permission -- if we cannot see whose service this touches, a human decides.
        findings.append("service_impact_unknown")
        needs_biz_approval = True
    elif int(impact.get("active_impacted", 0)) > int(
            biz.get("max_active_services_impacted_without_approval", 0)):
        findings.append(f"impacts_{impact.get('active_impacted')}_active_services")
        needs_biz_approval = True

    # 8. corroboration ---------------------------------------------------------------
    # An expensive, barely-reversible action must be supported by more than one plane. The
    # planes are mutually blind, so their agreement is evidence; a second look from the same
    # plane would only repeat that plane's failure mode.
    corr_cfg = (g.get("corroboration") or {})
    corr = (proposal.get("parameters", {}) or {}).get("corroboration") or {}
    if corr_cfg.get("enforce", True) and corr:
        required = int(corr.get("required", corr_cfg.get("default_required_planes", 1)))
        got = int(corr.get("witnesses", 0))
        if got < required:
            findings.append(f"uncorroborated_{got}_of_{required}_planes")
            needs_biz_approval = True

    # 9. licence -> who signs ------------------------------------------------------
    threshold = cls.get("require_approval_below", "supervised-auto")
    if in_crisis or threshold == "never" or needs_biz_approval:
        # 'never' means never auto-apply: a human signs at every trust state.
        mode = "human-signed"
        findings.append("human_approval_required")
    elif trust_state in AUTO_STATES and _at_least(trust_state, threshold):
        mode = "policy-auto"
    else:
        mode = "human-signed"
        findings.append("below_auto_threshold")

    return {
        "allowed": True,
        "mode": mode,
        "policy": f"guardrails.yaml#{fault_class}",
        "within_budget": True,
        "findings": findings,
        "force_dry_run": bool(in_crisis and (crisis.get("effect", {}) or {}).get("force_dry_run")),
    }


def _refuse(findings):
    return {
        "allowed": False,
        "mode": "refused",
        "policy": "guardrails.yaml",
        "within_budget": False,
        "findings": findings,
        "force_dry_run": True,
    }


def _at_least(state, threshold):
    try:
        return STATES.index(state) >= STATES.index(threshold)
    except ValueError:
        return False


if __name__ == "__main__":
    import json
    # Show the engine refusing and permitting, so the behaviour is readable without a cluster.
    demo = {
        "fault_class": "OC-NodeDown", "action": "redfish_power_on",
        "target": "BareMetalHost/bm-1", "parameters": {"nodes_touched": 1},
        "sandbox_verdict": {"apply_allowed": True, "rehearsed_at": "now", "twin_divergence": 0.0},
    }
    print("rehearsed  ->", json.dumps(evaluate(demo, "rehearsed")))
    print("supervised ->", json.dumps(evaluate(demo, "supervised-auto")))
    print("unrehearsed->", json.dumps(evaluate({**demo, "sandbox_verdict": None}, "governed-auto")))
    print("protected  ->", json.dumps(evaluate({**demo, "target": "BareMetalHost/bm-0"}, "governed-auto")))
    print("bad verb   ->", json.dumps(evaluate({**demo, "action": "rm_-rf"}, "governed-auto")))
