"""Lead-loop timing resolution (WP1).

A SINGLE source for a managed lead-loop agent's ``{ttl_seconds, cadence_seconds,
heartbeat_stale_after}`` so the controller's acquire/steal path AND the
doctor/status/list visibility path use the SAME ``heartbeat_stale_after``. If those
two diverged, the ``armed == not stealable`` invariant would break: a view could
declare an owner stuck/stealable at a different threshold than the steal path, and
the detector, steal, and guard would disagree (the exact drift class WP1 ends).

This is an ADAPTER-layer module: it reads the store and, optionally, a supervisor
config, keeping ``store.py`` free of supervisor imports (the pure-core / adapter
split). ``store.py`` never imports this module.
"""
from __future__ import annotations

from agenttalk.store import (
    ACTIVE_WITHIN_SECONDS,
    LEAD_LOOP_CADENCE_DEFAULT,
    LEAD_LOOP_TTL_DEFAULT,
)


def resolve_timing(store, agent: str, supervisor_config: dict | None = None) -> dict:
    """Resolve ``{ttl_seconds, cadence_seconds, heartbeat_stale_after}`` for ``agent``.

    ``ttl_seconds`` / ``cadence_seconds`` come from the ``managed_lead_loop`` spec
    (the configured lease bounds), falling back to the store defaults.

    ``heartbeat_stale_after`` is the CONTRACT value that BOTH the steal path
    (``acquire_lead_loop_lease`` / ``_lease_stealable``) AND the visibility path
    (``lead_loop_state`` / doctor / status / list) must pass, so they never disagree:

      - with ``supervisor_config`` (a wrapped / supervised agent): the supervisor's
        per-agent / per-CLI :func:`supervisor.resolve_stuck_after` - so a DUPLICATE
        controller never steals EARLIER than the supervisor would declare the owner
        stuck (the steal threshold equals the supervisor's stuck threshold);
      - without it: the store freshness default (:data:`ACTIVE_WITHIN_SECONDS`).
    """
    spec = store.managed_lead_loop_spec(agent) or {}
    ttl = float(spec.get("ttl_seconds", LEAD_LOOP_TTL_DEFAULT))
    cadence = float(spec.get("cadence_seconds", LEAD_LOOP_CADENCE_DEFAULT))
    heartbeat_stale_after = float(ACTIVE_WITHIN_SECONDS)
    if supervisor_config:
        # Imported lazily so the common store/CLI paths that never supervise do not
        # pull in the supervisor module (and store.py can never import it transitively).
        from agenttalk import supervisor as _sup
        agents = supervisor_config.get("agents")
        # Coerce to dict only when it IS a dict: a `... or {}` rescues only FALSY
        # values, so a TRUTHY non-dict per-agent entry (an operator typo like
        # {"agents": {"beta": "wrapped"}}) would slip through and crash
        # resolve_stuck_after's cfg_agent.get(...). Fail safe to the default instead.
        cfg_agent = agents.get(agent) if isinstance(agents, dict) else None
        cfg_agent = cfg_agent if isinstance(cfg_agent, dict) else {}
        heartbeat_stale_after = float(_sup.resolve_stuck_after(supervisor_config, cfg_agent))
    return {
        "ttl_seconds": ttl,
        "cadence_seconds": cadence,
        "heartbeat_stale_after": heartbeat_stale_after,
    }
