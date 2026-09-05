from __future__ import annotations

import logging
import os
from datetime import date, datetime
from typing import Any, Optional
from uuid import uuid4

import pytz

from .identity import agent_base


log = logging.getLogger("provy.sdk")

# The reserved payload key the server reads a forward claim out of (argus#747). It is lifted into
# its own column at ingest, so it survives trace-body offload and cannot be overwritten by a later
# reading of the same signal.
CLAIM_KEY = "provy_claim"


def _normalise_claim(claim: Any) -> Optional[list]:
    """Validate a forward claim, or return None so a malformed one is dropped rather than stored.

    ⛔ A CLAIM IS NOT A PAYLOAD KEY, AND THAT IS THE WHOLE POINT (argus#751). Provy keeps the LAST
    value it sees for a signal across a session, which is correct for a READING: a close step
    supersedes earlier partials and grading depends on it. It destroys a CLAIM. On the reference
    fleet an agent reported `realized_pnl: 0` during the run, a later step reported the settled
    figure under the same key, and Provy stored the settled figure as what the agent had claimed.
    Every non-zero "claimed" value on that fleet is a byte-for-byte copy of what actually settled.

    A claim in its own key does not compete with a reading, because nothing else writes that key.

    ⛔ `signal` MUST NAME THE KEY THE OUTCOME SETTLES UNDER. A claim filed under a name the contract
    does not grade never meets the outcome it is meant to be compared with, so it is worth nothing.
    If your contract grades `refund_posted`, claim `refund_posted`, not `expected_refund`.

    ⛔ DROPPED, NOT RAISED. Telemetry must never break the caller's run, and this is called from
    inside a logging path. A malformed claim is logged at warning and omitted.
    """
    if claim is None:
        return None
    items = claim if isinstance(claim, (list, tuple)) else [claim]
    out = []
    for c in items:
        if not isinstance(c, dict):
            log.warning("provy: claim ignored, expected a dict, got %s", type(c).__name__)
            continue
        signal = c.get("signal")
        if not isinstance(signal, str) or not signal.strip():
            log.warning("provy: claim ignored, it names no signal")
            continue
        if "value" not in c:
            log.warning("provy: claim on %r ignored, it states no value", signal)
            continue
        item: dict[str, Any] = {"signal": signal.strip(), "value": c["value"]}
        conf = c.get("confidence")
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            # Clamped rather than refused: a caller on a 0-100 scale is a docs failure, and dropping
            # their confidence is a worse answer than pinning it to the range.
            item["confidence"] = min(1.0, max(0.0, float(conf)))
        elif conf is not None:
            log.warning("provy: claim on %r has a non-numeric confidence, omitting it", signal)
        ent = c.get("entity_id")
        if isinstance(ent, str) and ent.strip():
            item["entity_id"] = ent.strip()
        out.append(item)
    return out or None


def _load_env(key: str) -> str:
    val = os.environ.get(key, "")
    if not val:
        try:
            from dotenv import load_dotenv as _ld
            _ld()
            val = os.environ.get(key, "")
        except ImportError:
            pass
    return val


def _emit_enabled() -> bool:
    """Whether the SDK may write telemetry to Provy.

    Off by default, so a local or dev run holding production credentials does not
    write into your production Provy (and never creates a leftover 'in progress'
    session row). Turn it on in your production or CI environment with PROVY_EMIT=1.
    When off, the TraceLogger is a no-op: no session stub, no trace rows, no compute
    trigger, so your code runs unchanged.
    """
    return _load_env("PROVY_EMIT").strip().lower() in ("1", "true", "yes", "on")


_COST_PER_MTOK: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00,  "cache_read": 0.08,  "cache_write": 1.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00, "cache_read": 0.30,  "cache_write": 3.75},
    "claude-opus-4-7":           {"input": 15.00, "output": 75.00, "cache_read": 1.50,  "cache_write": 18.75},
}


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float:
    rates = _COST_PER_MTOK.get(model, {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75})
    return (
        input_tokens       * rates["input"]       +
        output_tokens      * rates["output"]       +
        cache_read_tokens  * rates["cache_read"]   +
        cache_write_tokens * rates["cache_write"]
    ) / 1_000_000


class TraceLogger:
    """
    Writes structured trace rows to ag_traces and a summary row to ag_sessions.

    Quickstart:
        tracer = TraceLogger(session_id, workflow_id=os.environ["WORKFLOW_ID"])
        tracer.start_agent_span("research")
        tracer.log_tool_call("research", "web_search", {"query": "..."}, result, latency_ms=320)
        tracer.log_agent_message("research", reasoning, "approved", tokens_input=800, tokens_output=200, model="claude-haiku-4-5-20251001")
        tracer.close_session("completed", result_summary="3 trades executed")

    Environment variables required:
        PROVY_URL — your Provy deployment URL (e.g. https://provy.ai), or ARGUS_URL; enables instant diagnosis + embeddings on session close
        SUPABASE_URL   — your Supabase project URL
        SUPABASE_KEY   — your Supabase service role key
        TENANT_ID      — your Provy tenant UUID (from Settings)
        WORKFLOW_ID    — your Provy pipeline UUID (from Settings)
    """

    def __init__(
        self,
        session_id: str,
        workflow_id: Optional[str] = None,
        session_type: Optional[str] = None,
        default_model: str = "claude-haiku-4-5-20251001",
    ):
        self.session_id    = session_id
        self._tenant_id    = _load_env("TENANT_ID")
        self._workflow_id  = workflow_id or _load_env("WORKFLOW_ID") or None
        self._session_type = session_type
        self._default_model = default_model
        self._sequence     = 0
        self._agent_spans: dict[str, str] = {}
        self._tokens: dict[str, dict[str, int]] = {}
        self._started_at   = datetime.utcnow()
        self._enabled      = _emit_enabled()
        self._insert_session_stub()

    # ── Public API ──────────────────────────────────────────────────────────────

    def start_agent_span(self, agent: str) -> str:
        """Register a new span for this agent. Returns the new span_id."""
        span_id = str(uuid4())
        self._agent_spans[agent] = span_id
        return span_id

    def log_tool_call(
        self,
        agent: str,
        tool_name: str,
        tool_input: dict,
        tool_output: Any,
        entity_id: Optional[str] = None,
        latency_ms: int = 0,
        model: Optional[str] = None,
    ) -> str:
        """Write a tool_call row. Returns the new span_id."""
        return self._write({
            "step_type":   "tool_call",
            "agent":       agent,
            "tool_name":   tool_name,
            "tool_input":  tool_input,
            "tool_output": tool_output if isinstance(tool_output, dict) else {"value": tool_output},
            "entity_id":   entity_id,
            "latency_ms":  latency_ms,
            "model":       model,
        })

    def log_agent_message(
        self,
        agent: str,
        reasoning: str,
        outcome: str,
        entity_id: Optional[str] = None,
        tokens_input: int = 0,
        tokens_output: int = 0,
        model: Optional[str] = None,
        latency_ms: int = 0,
        payload: Optional[dict] = None,
        claim: Optional[Any] = None,
    ) -> str:
        """Write an agent_message row. Returns the new span_id.

        `payload` carries structured scalars alongside the prose. A number stated only inside
        `reasoning` is one blob of text to Provy: it cannot be read as a signal, bound to a contract
        condition, or compared with what settled.

        `claim` is this step's FORWARD CLAIM: `{"signal", "value", "confidence"?, "entity_id"?}` or a
        list of those. See `_normalise_claim` for why it is a separate field and not a payload key.
        """
        return self._write({
            "step_type":       "agent_message",
            "agent":           agent,
            "agent_reasoning": reasoning,
            "outcome":         outcome,
            "entity_id":       entity_id,
            "tokens_input":    tokens_input,
            "tokens_output":   tokens_output,
            "latency_ms":      latency_ms,
            "model":           model,
            "payload":         payload,
            "claim":           claim,
        })

    def log_decision(
        self,
        agent: str,
        outcome: str,
        detail: Optional[dict] = None,
        latency_ms: int = 0,
        model: Optional[str] = None,
        claim: Optional[Any] = None,
    ) -> str:
        """Write a session-level decision row. Returns span_id.

        `claim` states what this decision EXPECTS to happen, before the answer exists:
        `{"signal", "value", "confidence"?, "entity_id"?}` or a list of those.

        A forward claim is a decision with an assertion attached, so this is a parameter rather than
        a new concept and a new door. It is also accepted on `log_agent_message`, because a claim
        belongs on the span where the agent actually made it: forcing every claim onto a `decision`
        step would either misdescribe the act or make you log a second span for the same moment.
        """
        return self._write({
            "step_type":   "decision",
            "agent":       agent,
            "outcome":     outcome,
            "tool_output": detail,
            "latency_ms":  latency_ms,
            "model":       model,
            "claim":       claim,
        })

    def log_skip(
        self,
        agent: str,
        reason: str,
        skip_type: str = "design",
    ) -> str:
        """
        Record that an agent was intentionally skipped.

        Args:
            agent:     Base agent name (e.g. 'news', 'risk').
            reason:    Human-readable reason (e.g. 'no_candidates', 'upstream_data_missing').
            skip_type: 'design'  — expected routing (no alarm in Provy).
                       'error'   — upstream failure caused the skip (shown as amber in Provy).

        Returns span_id. Provy uses this to distinguish intentional routing
        from error propagation in the session diagnosis and agents list.
        """
        return self._write({
            "step_type": "skip",
            "agent":     agent,
            "outcome":   "skipped",
            "payload":   {"reason": reason, "skip_type": skip_type},
        })

    def log_error(
        self,
        agent: str,
        error_message: str,
        entity_id: Optional[str] = None,
    ) -> str:
        """Write an error row. Returns span_id."""
        return self._write({
            "step_type": "error",
            "agent":     agent,
            "error":     error_message,
            "entity_id": entity_id,
            "outcome":   "error",
        })

    def log_tokens(self, agent: str, usage: Any) -> None:
        """
        Accumulate token counts for an agent. Pass an Anthropic Usage object or a dict.
        Written to ag_sessions at close_session().
        """
        if hasattr(usage, "input_tokens"):
            inp = usage.input_tokens
            out = usage.output_tokens
            cr  = getattr(usage, "cache_read_input_tokens",    0) or 0
            cw  = getattr(usage, "cache_creation_input_tokens", 0) or 0
        else:
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            cr  = usage.get("cache_read_input_tokens",    0) or 0
            cw  = usage.get("cache_creation_input_tokens", 0) or 0

        if agent not in self._tokens:
            self._tokens[agent] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        self._tokens[agent]["input"]       += inp
        self._tokens[agent]["output"]      += out
        self._tokens[agent]["cache_read"]  += cr
        self._tokens[agent]["cache_write"] += cw

    def close_session(
        self,
        terminal_reason: str,
        agents_invoked: Optional[list[str]] = None,
        loop_iterations: int = 1,
        trades_proposed: int = 0,
        trades_approved: int = 0,
        trades_executed: int = 0,
        risk_rejections: int = 0,
        retry_triggered: bool = False,
        result_summary: Optional[str] = None,
    ) -> None:
        """Finalize the ag_sessions row for this session."""
        if not self._enabled:
            return  # emission off: no-op
        from provy.db import get_client

        completed_at = datetime.utcnow()
        latency_ms   = int((completed_at - self._started_at).total_seconds() * 1000)

        metadata: dict[str, Any] = {
            "date":             date.today().isoformat(),
            "total_steps":      self._sequence,
            "trades_proposed":  trades_proposed,
            "trades_approved":  trades_approved,
            "trades_executed":  trades_executed,
            "risk_rejections":  risk_rejections,
            "retry_triggered":  retry_triggered,
            "total_latency_ms": latency_ms,
        }

        row: dict[str, Any] = {
            "terminal_reason": terminal_reason,
            "ended_at":        completed_at.isoformat(),
            "status":          "completed",
            "metadata":        metadata,
        }
        if result_summary:
            row["result_summary"] = result_summary

        if self._tokens:
            agent_costs: dict[str, Any] = {}
            for agent, v in self._tokens.items():
                model = self._default_model
                cost  = _estimate_cost(model, v["input"], v["output"],
                                       v.get("cache_read", 0), v.get("cache_write", 0))
                agent_costs[agent] = {
                    "model":       model,
                    "input":       v["input"],
                    "output":      v["output"],
                    "cache_read":  v.get("cache_read",  0),
                    "cache_write": v.get("cache_write", 0),
                    "cost_usd":    round(cost, 6),
                }
            total_cost   = sum(a["cost_usd"] for a in agent_costs.values())
            total_input  = sum(v["input"]    for v in self._tokens.values())
            total_output = sum(v["output"]   for v in self._tokens.values())
            row.update({
                "total_tokens_input":  total_input,
                "total_tokens_output": total_output,
                "total_cost_usd":      round(total_cost, 6),
            })
            metadata.update({
                "agents_invoked":   agents_invoked or list(self._tokens.keys()),
                "loop_iterations":  loop_iterations,
                "cost_breakdown":   agent_costs,
            })

        get_client().table("ag_sessions").update(row).eq("id", self.session_id).execute()
        self._trigger_provy_compute()

    def get_sequence(self) -> int:
        return self._sequence

    def get_agent_span(self, agent: str) -> Optional[str]:
        return self._agent_spans.get(agent)

    # ── Private ─────────────────────────────────────────────────────────────────

    def _trigger_provy_compute(self) -> None:
        """Fire-and-forget: trigger diagnosis + embeddings on Provy immediately after close.
        Requires PROVY_URL (or ARGUS_URL) env var. No-op if unset or emission is off."""
        if not getattr(self, "_enabled", False):
            return
        import threading
        import json
        import urllib.request

        provy_url = (_load_env("PROVY_URL") or _load_env("ARGUS_URL")).rstrip("/")
        if not provy_url or not self._tenant_id or not self._workflow_id:
            return

        payload = json.dumps({
            "session_id":  self.session_id,
            "tenant_id":   self._tenant_id,
            "workflow_id": self._workflow_id,
        }).encode()

        def _post(path: str) -> None:
            try:
                req = urllib.request.Request(
                    f"{provy_url}{path}",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=30)
            except Exception as exc:  # noqa: BLE001
                # Genuinely non-fatal: this only NUDGES a server-side compute job, it does not carry
                # data. The job also runs on its own schedule, so a missed nudge delays a derived
                # view rather than losing anything.
                #
                # Logged all the same. "Non-fatal" is a reason not to raise, never a reason to be
                # invisible.
                log.warning("provy: could not trigger %s: %s", path, exc)

        threading.Thread(target=_post, args=("/api/compute/diagnoses",), daemon=True).start()
        threading.Thread(target=_post, args=("/api/compute/embeddings",), daemon=True).start()

    def _insert_session_stub(self) -> None:
        if not self._enabled:
            return  # emission off: do not create a session row
        from provy.db import get_client
        stub: dict[str, Any] = {
            "id":              self.session_id,
            "tenant_id":       self._tenant_id,
            "workflow_id":     self._workflow_id,
            "started_at":      self._started_at.isoformat(),
            "status":          "in_progress",
            "terminal_reason": "in_progress",
        }
        if self._session_type:
            stub["session_type"] = self._session_type
        get_client().table("ag_sessions").upsert(stub, on_conflict="id", ignore_duplicates=True).execute()

    def _write(self, fields: dict) -> str:
        if not self._enabled:
            return str(uuid4())  # emission off: local span id, no DB write
        from provy.db import get_client
        span_id = str(uuid4())
        self._sequence += 1
        agent     = fields.get("agent", "orchestrator")
        entity_id = fields.get("entity_id")

        tokens_input  = fields.get("tokens_input", 0)
        tokens_output = fields.get("tokens_output", 0)
        model         = fields.get("model") or self._default_model
        cost_usd: Optional[float] = None
        if tokens_input or tokens_output:
            cost_usd = round(_estimate_cost(model, tokens_input, tokens_output), 8)

        payload: dict[str, Any] = {
            "span_id":        span_id,
            # Same base-name rule as the OTel path; the REST payload carried the same bug (#668).
            "parent_span_id": self._agent_spans.get(agent_base(agent) or agent)
                              or self._agent_spans.get(agent),
            "entity_id":      entity_id,
            "date":           date.today().isoformat(),
            "sequence":       self._sequence,
            "model":          model,
        }
        if fields.get("tool_input")      is not None: payload["tool_input"]      = fields["tool_input"]
        if fields.get("tool_output")     is not None: payload["tool_output"]     = fields["tool_output"]
        if fields.get("agent_reasoning") is not None: payload["agent_reasoning"] = fields["agent_reasoning"]
        if fields.get("payload")         is not None: payload.update(fields["payload"])
        # ⛔ WRITTEN LAST, SO A payload KEY CANNOT SHADOW THE CLAIM. `payload.update()` above merges
        # caller-supplied keys, and a caller passing `provy_claim` in `payload` would otherwise
        # silently replace a validated claim with an unvalidated one.
        claim = _normalise_claim(fields.get("claim"))
        if claim is not None:
            payload[CLAIM_KEY] = claim[0] if len(claim) == 1 else claim

        row: dict[str, Any] = {
            "tenant_id":     self._tenant_id,
            "session_id":    self.session_id,
            "agent":         agent,
            "step_type":     fields.get("step_type"),
            "tool_name":     fields.get("tool_name"),
            "outcome":       fields.get("outcome"),
            "error":         fields.get("error"),
            "latency_ms":    fields.get("latency_ms", 0),
            "tokens_input":  tokens_input,
            "tokens_output": tokens_output,
            "cost_usd":      cost_usd,
            "payload":       payload,
            "created_at":    datetime.utcnow().isoformat(),
        }
        get_client().table("ag_traces").insert(row).execute()
        return span_id
