"""The licence ledger: autonomy per fault class, earned on evidence and revoked on failure.

Two files, on purpose:

  harness/sheldon/trust-registry.yaml     POLICY   human-authored, commented, committed.
                                                   Seeds each class, and sets the CEILING
                                                   that no run count may ever lift.
  harness/sheldon/state/trust-state.json  STATUS   machine-written. Evidence counters,
                                                   current state, last demotion.

Splitting them keeps the policy diff-able (a change is a human decision, visible in git)
while the evidence accrues underneath without rewriting the commented contract.

The asymmetry this module enforces:

  PROMOTION  is a proposal, never an act. record_run() accumulates evidence and
             promotion_case() reports when a class has met the bar — but the state only
             moves when a human calls promote(). Ratification stays human.
  DEMOTION   is an act, never a proposal. demote() fires on any trigger from
             demotion_policy, immediately, with no meeting. At 3 a.m., with a degraded
             network, nobody should have to argue for revoking autonomy from a system
             the organisation leans on. The control system revokes it, on evidence.

A grade that rises slowly and falls automatically gets SAFER when something breaks.
"""

import json
import os
from datetime import datetime, timezone

import yaml

HARNESS_DIR = os.path.abspath(os.environ.get(
    "SHELDON_HARNESS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "harness", "sheldon")))
REGISTRY = os.path.join(HARNESS_DIR, "trust-registry.yaml")
STATE_DIR = os.path.join(HARNESS_DIR, "state")
STATE = os.path.join(STATE_DIR, "trust-state.json")

STATES = ["observed", "advised", "rehearsed", "supervised-auto", "governed-auto"]
AUTO_STATES = {"supervised-auto", "governed-auto"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_registry(path=REGISTRY):
    with open(path) as fh:
        return yaml.safe_load(fh)


def load_state():
    """Live licence state, seeded from the policy file on first run.

    Also MERGES FORWARD: a fault class added to trust-registry.yaml after the state file
    exists is seeded here on next read, at the policy's starting state. Without this a new
    class silently falls back to `observed` (the unknown-class default) and the operator
    sees a licence they never wrote — which is exactly what happened when OC-ServiceDegraded
    was added. Existing classes are never overwritten: earned evidence outranks the seed.
    """
    if os.path.exists(STATE):
        with open(STATE) as fh:
            state = json.load(fh)
        reg = load_registry()
        added = False
        for c in reg.get("classes", []):
            if c["fault_class"] not in state.get("classes", {}):
                state.setdefault("classes", {})[c["fault_class"]] = {
                    "trust_state": c["trust_state"],
                    "ceiling": c["ceiling"],
                    "demote_to": c.get("demote_to", "advised"),
                    "evidence": dict(c.get("evidence", {})),
                    "history": [{"at": _now(), "seeded_from": "trust-registry.yaml"}],
                }
                added = True
        if added:
            save_state(state)
        return state
    reg = load_registry()
    state = {
        "ocloud": reg.get("metadata", {}).get("ocloud", "sheldon"),
        "seeded_at": _now(),
        "classes": {
            c["fault_class"]: {
                "trust_state": c["trust_state"],
                "ceiling": c["ceiling"],
                "demote_to": c.get("demote_to", "advised"),
                "evidence": dict(c.get("evidence", {})),
                "history": [],
            } for c in reg.get("classes", [])
        },
    }
    save_state(state)
    return state


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=False)
        fh.write("\n")


def entry(fault_class, state=None):
    state = state or load_state()
    return state["classes"].get(fault_class)


def state_for(fault_class):
    """The licence this class holds right now. Unknown classes get the weakest state."""
    e = entry(fault_class)
    return e["trust_state"] if e else "observed"


def may_apply(fault_class):
    """Does the licence alone permit unattended application? The guardrail still has a
    veto — this answers only the licence half of the question."""
    return state_for(fault_class) in AUTO_STATES


def record_run(fault_class, *, verified, divergence=0.0, trigger=None, detail=""):
    """Write one run into the ledger. This is where autonomy is earned or lost.

    verified    did the action actually clear the fault, checked against the same
                sources that raised it? An unverified run never counts as evidence.
    trigger     a demotion_policy trigger, if one fired. Any trigger demotes, now.
    """
    st = load_state()
    e = st["classes"].setdefault(fault_class, {
        "trust_state": "observed", "ceiling": "advised",
        "demote_to": "observed", "evidence": {}, "history": []})
    ev = e.setdefault("evidence", {})
    outcome = {"at": _now(), "verified": verified, "divergence": divergence,
               "trigger": trigger, "detail": detail[:200]}

    demoted = False
    if trigger or not verified or float(divergence or 0.0) != 0.0:
        before = e["trust_state"]
        e["trust_state"] = e.get("demote_to", "advised")
        ev["clean_runs_at_state"] = 0
        ev["last_eval"] = "fail"
        demoted = before != e["trust_state"]
        outcome["demoted_from"] = before
        outcome["demoted_to"] = e["trust_state"]
        outcome["trigger"] = trigger or ("verification_failed" if not verified
                                         else "twin_divergence")
    else:
        ev["clean_runs_at_state"] = int(ev.get("clean_runs_at_state", 0)) + 1
        ev["last_eval"] = "pass"
    ev["twin_live_divergence"] = float(divergence or 0.0)
    ev["last_run"] = outcome["at"]
    e.setdefault("history", []).append(outcome)
    e["history"] = e["history"][-50:]
    save_state(st)
    return {"demoted": demoted, "trigger": outcome.get("trigger"),
            "state_after": e["trust_state"],
            "clean_runs_at_state": ev.get("clean_runs_at_state", 0)}


def record_rollback_drill(fault_class, passed=True):
    """A rehearsed inverse that actually restored prior state. Counts toward promotion;
    a failed drill is a reversibility_miss and demotes immediately."""
    if not passed:
        return record_run(fault_class, verified=False, trigger="reversibility_miss",
                          detail="rollback drill failed")
    st = load_state()
    e = st["classes"].setdefault(fault_class, {"evidence": {}})
    ev = e.setdefault("evidence", {})
    ev["rollback_drills_passed"] = int(ev.get("rollback_drills_passed", 0)) + 1
    save_state(st)
    return {"rollback_drills_passed": ev["rollback_drills_passed"]}


def promotion_case(fault_class):
    """Report whether this class has MET THE BAR for promotion. Reporting only: the
    state does not move until a human calls promote()."""
    reg = load_registry()
    pol = reg.get("promotion_policy", {})
    e = entry(fault_class)
    if not e:
        return {"eligible": False, "reason": "unknown_class"}
    ev = e.get("evidence", {})
    at_ceiling = e["trust_state"] == e.get("ceiling")
    reasons = []
    if at_ceiling:
        reasons.append("at_ceiling")
    if int(ev.get("clean_runs_at_state", 0)) < int(pol.get("min_clean_runs_at_current_state", 10)):
        reasons.append("insufficient_clean_runs")
    if int(ev.get("rollback_drills_passed", 0)) < int(pol.get("min_rollback_drills_passed", 3)):
        reasons.append("insufficient_rollback_drills")
    if float(ev.get("twin_live_divergence", 0.0)) > float(pol.get("max_twin_live_divergence", 0.0)):
        reasons.append("twin_divergence")
    return {
        "eligible": not reasons,
        "blocking": reasons,
        "current": e["trust_state"],
        "ceiling": e.get("ceiling"),
        "next": _next_state(e["trust_state"], e.get("ceiling")),
        "requires_human_ratification": bool(pol.get("require_human_ratification", True)),
        "evidence": ev,
    }


def promote(fault_class, ratified_by):
    """Move a class up one state. Refuses to exceed the ceiling — the ceiling comes from
    the class's reversibility profile, not its track record, so no volume of clean runs
    can lift a class whose action has no inverse."""
    case = promotion_case(fault_class)
    if not case.get("eligible"):
        return {"promoted": False, "reason": case.get("blocking") or case.get("reason")}
    st = load_state()
    e = st["classes"][fault_class]
    nxt = _next_state(e["trust_state"], e.get("ceiling"))
    if nxt == e["trust_state"]:
        return {"promoted": False, "reason": ["at_ceiling"]}
    before = e["trust_state"]
    e["trust_state"] = nxt
    e["evidence"]["clean_runs_at_state"] = 0      # earn the next state from zero
    e.setdefault("history", []).append(
        {"at": _now(), "promoted_from": before, "promoted_to": nxt, "ratified_by": ratified_by})
    save_state(st)
    return {"promoted": True, "from": before, "to": nxt, "ratified_by": ratified_by}


def demote(fault_class, trigger, detail=""):
    """Instant, mechanical, automatic. No meeting required."""
    return record_run(fault_class, verified=False, trigger=trigger, detail=detail)


def _next_state(current, ceiling):
    try:
        i = STATES.index(current)
        cap = STATES.index(ceiling) if ceiling in STATES else i
    except ValueError:
        return current
    return STATES[min(i + 1, cap)]


def summary():
    """The whole ledger, for the console and for `kubectl`-style eyeballing."""
    st = load_state()
    rows = []
    for name, e in st["classes"].items():
        ev = e.get("evidence", {})
        rows.append({
            "fault_class": name,
            "trust_state": e["trust_state"],
            "ceiling": e.get("ceiling"),
            "clean_runs": ev.get("clean_runs_at_state", 0),
            "rollback_drills": ev.get("rollback_drills_passed", 0),
            "last_eval": ev.get("last_eval", "never"),
            "last_run": ev.get("last_run"),
        })
    return sorted(rows, key=lambda r: r["fault_class"])


if __name__ == "__main__":
    for row in summary():
        print(f"{row['fault_class']:<24} {row['trust_state']:<16} "
              f"ceiling={row['ceiling']:<16} clean={row['clean_runs']:<3} "
              f"drills={row['rollback_drills']:<3} last={row['last_eval']}")
