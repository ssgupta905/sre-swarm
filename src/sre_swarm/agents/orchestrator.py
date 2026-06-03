"""SwarmOrchestrator: runs the agent pipeline sequentially.

Phase 1 wires only the Triage agent. Later phases extend the same `run`
method with RCA, Chaos Replay, Predict, Heal (per FR-031).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from ..config import Config
from ..models import (
    Incident,
    IncidentStatus,
    PredictResult,
    RCAResult,
    RemediationPlan,
    ReplayResult,
    TriageResult,
)
from ..tools.chaos import configure_chaos
from ..tools.code import configure_code
from ..tools.db import configure_db_endpoints
from ..tools.kubectl import configure_kubectl
from ..tools.prometheus import configure_prometheus
from ..tools.server import get_tool_registry
from ..tools.splunk import configure_splunk
from .chaos_replay import build_chaos_replay_user_prompt
from .ollama_client import OllamaClient, run_tool_loop
from .events import (
    AgentMessage,
    AgentPhaseChange,
    AgentToken,
    AgentToolCall,
    AgentToolResult,
    HitlRequest,
    PipelineDone,
    RemediationPlanReady,
    UserMessage as UserChatMessage,
)
from .heal import ApprovalDecision
from .heal import (
    annotate_requires_individual,
    build_heal_execute_prompt,
    build_heal_plan_user_prompt,
)
from .predict import build_predict_user_prompt
from .prompts import (
    AGENT_TOOLS,
    CHAOS_REPLAY_SYSTEM_PROMPT,
    HEAL_PLAN_SYSTEM_PROMPT,
    PREDICT_SYSTEM_PROMPT,
    RCA_SYSTEM_PROMPT,
    TRIAGE_SYSTEM_PROMPT,
)
from .rca import build_rca_user_prompt
from .triage import build_triage_user_prompt


HitlCallback = Callable[[object], Awaitable[object]]
GuidanceProvider = Callable[[str], list]


class SwarmOrchestrator:
    def __init__(
        self,
        config: Config,
        hitl_callback: Optional[HitlCallback] = None,
        event_bus: Optional[asyncio.Queue] = None,
        guidance_provider: Optional[GuidanceProvider] = None,
    ) -> None:
        self.config = config
        self.hitl_callback = hitl_callback
        self.event_bus = event_bus or asyncio.Queue()
        self.guidance_provider = guidance_provider
        configure_prometheus(config.prometheus.url, config.prometheus.timeout_s)
        configure_splunk(
            config.splunk.url, config.splunk.token, config.splunk.timeout_s
        )
        configure_kubectl(config.cluster.context, config.cluster.namespace)
        configure_chaos(
            {s.name: s.chaos_api for s in config.services if s.chaos_api},
            sandbox_namespace=config.cluster.sandbox_namespace,
            demo_namespace=config.cluster.namespace,
        )
        configure_code(
            os.environ.get("SRE_SWARM_CODE_REPO") or config.code.repo_root or None
        )
        configure_db_endpoints(
            {s.name: s.db_api for s in config.services if s.db_api},
        )
        self._tools_read = get_tool_registry(include_write=False)
        self._tools_write = get_tool_registry(include_write=True)
        self._llm = OllamaClient(
            url=config.ollama.url,
            model=config.ollama.model,
            timeout_s=config.ollama.timeout_s,
            keep_alive=config.ollama.keep_alive,
        )
        self._max_turns = config.ollama.max_turns

    async def run(
        self,
        incident: Incident,
        description: Optional[str] = None,
    ) -> Incident:
        """DAG execution — strictly sequential after the RCA gate:

              triage
                │
              ┌─┴──┐
             rca  predict   (parallel)
              └─┬──┘
        ┌── rca_confirm (HITL) ──┐  if rejected: stop here
        │
        chaos_replay  (sandbox auto — validates the hypothesis FIRST)
        │
        heal_plan
        │
        heal approval (HITL) ──── if rejected: stop here
        │
        heal_execute  (live, on prod cluster)
        """
        incident.status = IncidentStatus.INVESTIGATING
        incident.updated_at = datetime.now(timezone.utc)
        try:
            await self._guarded("triage", self._run_triage(incident, description), incident.id)

            await self._guarded("rca", self._run_rca(incident), incident.id)
            await self._guarded("predict", self._run_predict(incident), incident.id)

            # Human gate #1: operator confirms the root cause. If approved,
            # sandbox replay + (per-step) heal-execute are both authorized.
            rca_confirmed = await self._request_approval(
                incident,
                kind="rca_confirm",
                summary=_rca_confirm_summary(incident),
            )
            if not rca_confirmed:
                await self._emit_done(incident.id)
                return incident

            # Strictly sequential: validate hypothesis in sandbox FIRST,
            # then generate the heal plan, then (after HITL) execute it.
            await self._guarded("chaos_replay", self._run_chaos_replay(incident), incident.id)
            await self._guarded("heal", self._run_heal(incident), incident.id)
        except asyncio.CancelledError:
            await self._emit("pipeline", "paused", incident_id=incident.id)
            raise
        await self._emit_done(incident.id)
        return incident

    async def _guarded(self, name: str, coro, incident_id: Optional[str] = None) -> None:
        """Run a stage coroutine, surfacing exceptions as `error` phase events."""
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("SRE_SWARM_DEBUG"):
                import traceback
                sys.stderr.write(
                    f"[debug] stage {name} raised: {exc!r}\n"
                    f"{traceback.format_exc()}\n"
                )
            await self._emit(name, "error", str(exc), incident_id=incident_id)

    async def _run_triage(self, incident: Incident, description: Optional[str]) -> None:
        await self._emit("triage", "running", incident_id=incident.id)
        raw = await self._invoke_agent(
            agent_name="triage",
            system=TRIAGE_SYSTEM_PROMPT,
            user_prompt=build_triage_user_prompt(incident, description),
            incident_id=incident.id,
        )
        triage = _parse_json(raw, TriageResult)
        incident.triage = triage
        if triage is not None:
            incident.urgency = triage.urgency
        await self._emit("triage", "done", triage, incident_id=incident.id)

    async def _run_rca(self, incident: Incident) -> None:
        await self._emit("rca", "running", incident_id=incident.id)
        raw = await self._invoke_agent(
            agent_name="rca",
            system=RCA_SYSTEM_PROMPT,
            user_prompt=build_rca_user_prompt(incident),
            incident_id=incident.id,
        )
        rca = _parse_json(raw, RCAResult)
        incident.rca = rca
        await self._emit("rca", "done", rca, incident_id=incident.id)

    async def _run_chaos_replay(self, incident: Incident) -> None:
        # No per-stage gate — authorized upstream by the rca_confirm checkpoint.
        # Operates strictly inside the sandbox namespace.
        await self._emit("chaos_replay", "running", incident_id=incident.id)
        base_prompt = build_chaos_replay_user_prompt(
            incident, self.config.cluster.sandbox_namespace
        )
        replay = None
        for attempt in range(2):
            user_prompt = base_prompt
            if attempt > 0:
                user_prompt = (
                    "RETRY: your previous reply skipped the action. You MUST call "
                    "chaos_inject on the affected service NOW, then prometheus_query, "
                    "then chaos_reset. scenario_applied='none'/'no-scenario' is forbidden.\n\n"
                    + base_prompt
                )
            raw = await self._invoke_agent(
                agent_name="chaos_replay",
                system=CHAOS_REPLAY_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                incident_id=incident.id,
            )
            replay = _parse_json(raw, ReplayResult)
            if not _replay_skipped(replay):
                break
            if attempt == 0:
                await self.event_bus.put(AgentMessage(
                    agent="chaos_replay",
                    text="# retry: previous attempt produced no scenario — re-prompting",
                    incident_id=incident.id,
                ))
        incident.replay = replay
        await self._emit("chaos_replay", "done", replay, incident_id=incident.id)

    async def _run_predict(self, incident: Incident) -> None:
        await self._emit("predict", "running", incident_id=incident.id)
        raw = await self._invoke_agent(
            agent_name="predict",
            system=PREDICT_SYSTEM_PROMPT,
            user_prompt=build_predict_user_prompt(incident),
            incident_id=incident.id,
        )
        predict = _parse_json(raw, PredictResult)
        incident.predict = predict
        await self._emit("predict", "done", predict, incident_id=incident.id)

    async def _run_heal(self, incident: Incident) -> None:
        """Generate the plan only. Execution happens after HITL approval."""
        await self._emit("heal", "running", incident_id=incident.id)
        raw = await self._invoke_agent(
            agent_name="heal",
            system=HEAL_PLAN_SYSTEM_PROMPT,
            user_prompt=build_heal_plan_user_prompt(incident),
            incident_id=incident.id,
        )
        plan = _parse_json(raw, RemediationPlan)
        if plan is not None:
            plan = annotate_requires_individual(plan)
            plan = _enforce_plan_grounding(plan, incident, self.config.cluster.namespace)
        incident.plan = plan
        if plan is not None and self.hitl_callback is not None:
            await self._emit("heal", "waiting", plan, incident_id=incident.id)
            await self.event_bus.put(
                RemediationPlanReady(incident_id=incident.id, plan=plan)
            )
            req = HitlRequest(
                incident_id=incident.id, kind="heal", plan=plan,
                summary=_heal_summary(incident, plan),
            )
            await self.event_bus.put(req)
            try:
                decision = await self.hitl_callback(req)
            except Exception as exc:  # noqa: BLE001
                await self._emit("heal", "error", str(exc), incident_id=incident.id)
                return
            approved_steps: list = []
            rejected = False
            rejection_reason: Optional[str] = None
            if isinstance(decision, ApprovalDecision):
                approved_steps = list(decision.approved_steps or [])
                rejected = decision.rejected
                rejection_reason = decision.rejection_reason
            terminal_detail = {
                "plan": plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan,
                "approved": not rejected,
                "approved_steps": approved_steps,
                "rejected": rejected,
                "rejection_reason": rejection_reason,
            }
            await self._emit("heal", "done", terminal_detail, incident_id=incident.id)
            # If at least one step was approved, execute it against the live
            # cluster. Streamed tool_call / tool_result events let the UI tail
            # the kubectl invocations in real time.
            if not rejected and approved_steps:
                await self._run_heal_execute(incident, plan, approved_steps)
        else:
            await self._emit("heal", "done", plan, incident_id=incident.id)

    async def _run_heal_execute(
        self,
        incident: Incident,
        plan: RemediationPlan,
        approved_steps: list,
    ) -> None:
        """Iterate each approved step. Let the LLM pick the right write tool,
        but if it goes a full session without calling one, fall back to running
        the plan's literal kubectl command. Always emit a human summary at the end."""
        steps_by_num = {s.step_number: s for s in (plan.steps or [])}
        executed: list[dict] = []
        await self._emit("heal_execute", "running", incident_id=incident.id)
        service = (incident.event.service if incident.event else None) or "unknown-service"
        namespace = (incident.event.namespace if incident.event else None) or self.config.cluster.namespace

        for num in approved_steps:
            step = steps_by_num.get(int(num))
            if step is None:
                continue
            await self.event_bus.put(AgentMessage(
                agent="heal_execute",
                text=f"# step {step.step_number}: {step.action}",
                incident_id=incident.id,
            ))

            tool_calls_made: list[dict] = []
            tool_results_seen: list[dict] = []

            def _record_call(name: str, args: dict) -> None:
                tool_calls_made.append({"name": name, "args": args})

            def _record_result(name: str, ok: bool) -> None:
                tool_results_seen.append({"name": name, "ok": ok})

            try:
                raw = await self._invoke_agent(
                    agent_name="heal_execute",
                    system=build_heal_execute_prompt(step, service, namespace),
                    user_prompt=(
                        f"Execute step #{step.step_number} now. Target service: {service}. "
                        f"Namespace: {namespace}. Use the suggested kubectl command if available."
                    ),
                    incident_id=incident.id,
                    on_tool_call_hook=_record_call,
                    on_tool_result_hook=_record_result,
                )
            except Exception as exc:  # noqa: BLE001
                executed.append({
                    "step_number": step.step_number,
                    "action": step.action,
                    "executed": False,
                    "error": str(exc),
                })
                continue

            ok_calls = [r for r in tool_results_seen if r["ok"]]
            err_calls = [r for r in tool_results_seen if not r["ok"]]

            if not tool_calls_made and step.kubectl_command:
                # LLM stalled — run the planned command literally so the step doesn't silently no-op.
                fallback = await self._fallback_execute_step(step, service, namespace, incident.id)
                executed.append({
                    "step_number": step.step_number,
                    "action": step.action,
                    "executed": fallback["executed"],
                    "tool": fallback["tool"],
                    "result": fallback["result"],
                    "via": "fallback",
                })
                continue

            executed.append({
                "step_number": step.step_number,
                "action": step.action,
                "executed": bool(ok_calls) and not err_calls,
                "calls": len(tool_calls_made),
                "ok": len(ok_calls),
                "err": len(err_calls),
                "raw": raw[:240] if isinstance(raw, str) else "",
            })

        # Final human-readable summary so the operator never has to read raw JSON.
        summary = _heal_execute_summary(executed)
        await self.event_bus.put(AgentMessage(
            agent="heal_execute", text=summary, incident_id=incident.id,
        ))
        await self._emit(
            "heal_execute",
            "done",
            {"executed": executed, "count": len(executed), "summary": summary},
            incident_id=incident.id,
        )

    async def _fallback_execute_step(
        self, step, service: str, namespace: str, incident_id: str,
    ) -> dict:
        """If the LLM didn't call any tool, run the plan's literal command via
        the matching kubectl_write tool. Emits the same tool_call / tool_result
        events the LLM path would have, so the UI terminal stays consistent."""
        cmd = (step.kubectl_command or "").strip()
        if not cmd:
            return {"executed": False, "tool": "", "result": "no kubectl_command in plan"}
        # Pick the right write tool from the literal command.
        parts = cmd.split()
        registry = self._tools_write
        tool_name = ""
        args: dict[str, Any] = {}
        if "rollout" in parts and "restart" in parts:
            tool_name = "kubectl_rollout_restart"
            # last token after deployment/ — or the bare service name
            target = next((p for p in parts if "/" in p or p.endswith("-service")), service)
            args = {"deployment": target.split("/")[-1], "namespace": namespace}
        elif "scale" in parts:
            tool_name = "kubectl_scale"
            target = next((p for p in parts if "deployment/" in p), f"deployment/{service}")
            replicas = 1
            for p in parts:
                if p.startswith("--replicas"):
                    try:
                        replicas = int(p.split("=", 1)[1])
                    except (IndexError, ValueError):
                        pass
            args = {"deployment": target.split("/")[-1], "namespace": namespace, "replicas": replicas}
        else:
            return {"executed": False, "tool": "", "result": f"no fallback handler for: {cmd}"}

        fn = registry.get(tool_name)
        if fn is None:
            return {"executed": False, "tool": tool_name, "result": "tool not registered"}

        tool_id = f"fallback_{step.step_number}"
        await self.event_bus.put(AgentToolCall(
            agent="heal_execute", tool_id=tool_id, tool_name=tool_name,
            input=dict(args), incident_id=incident_id,
        ))
        try:
            result = await fn(args)
        except Exception as exc:  # noqa: BLE001
            result = {"isError": True, "content": [{"type": "text", "text": f"{tool_name} raised: {exc!r}"}]}
        is_error = bool(isinstance(result, dict) and result.get("isError"))
        await self.event_bus.put(AgentToolResult(
            agent="heal_execute", tool_id=tool_id,
            content=_serialize_tool_content(result.get("content") if isinstance(result, dict) else result),
            is_error=is_error, incident_id=incident_id,
        ))
        msg = ""
        if isinstance(result, dict):
            content = result.get("content") or []
            if content and isinstance(content, list):
                msg = str(content[0].get("text", ""))[:200]
        return {"executed": not is_error, "tool": tool_name, "result": msg}

    async def _request_approval(
        self, incident: Incident, kind: str, summary: str,
    ) -> bool:
        """Block until the operator decides. Returns True iff approved."""
        if self.hitl_callback is None:
            return True
        await self._emit(kind, "waiting", {"summary": summary}, incident_id=incident.id)
        req = HitlRequest(
            incident_id=incident.id, kind=kind, summary=summary,
        )
        await self.event_bus.put(req)
        try:
            decision = await self.hitl_callback(req)
        except Exception as exc:  # noqa: BLE001
            await self._emit(kind, "error", str(exc), incident_id=incident.id)
            return False
        if isinstance(decision, ApprovalDecision):
            return not decision.rejected
        return True

    def _augment_with_guidance(self, incident_id: str, user_prompt: str) -> str:
        if self.guidance_provider is None:
            return user_prompt
        try:
            notes = list(self.guidance_provider(incident_id) or [])
        except Exception:  # noqa: BLE001
            notes = []
        if not notes:
            return user_prompt
        block = "\n".join(f"- {note}" for note in notes)
        return (
            f"{user_prompt}\n\n"
            f"OPERATOR GUIDANCE (live, from copilot chat — treat as authoritative):\n"
            f"{block}\n"
        )

    async def _invoke_agent(
        self,
        agent_name: str,
        system: str,
        user_prompt: str,
        incident_id: Optional[str] = None,
        on_tool_call_hook: Optional[Callable[[str, dict], None]] = None,
        on_tool_result_hook: Optional[Callable[[str, bool], None]] = None,
    ) -> str:
        if incident_id:
            user_prompt = self._augment_with_guidance(incident_id, user_prompt)

        registry = self._tools_write if agent_name == "heal_execute" else self._tools_read
        allowed = AGENT_TOOLS.get(agent_name, [])
        tool_fns = [registry[name] for name in allowed if name in registry]

        _name_by_id: dict[str, str] = {}

        async def _on_tool_call(tool_id: str, tool_name: str, args: dict) -> None:
            _name_by_id[tool_id] = tool_name
            if on_tool_call_hook is not None:
                try:
                    on_tool_call_hook(tool_name, dict(args or {}))
                except Exception:  # noqa: BLE001
                    pass
            await self.event_bus.put(AgentToolCall(
                agent=agent_name,
                tool_id=tool_id,
                tool_name=tool_name,
                input=dict(args or {}),
                incident_id=incident_id,
            ))

        async def _on_tool_result(tool_id: str, result: Any, is_error: bool) -> None:
            if on_tool_result_hook is not None:
                try:
                    on_tool_result_hook(_name_by_id.get(tool_id, ""), not is_error)
                except Exception:  # noqa: BLE001
                    pass
            await self.event_bus.put(AgentToolResult(
                agent=agent_name,
                tool_id=tool_id,
                content=_serialize_tool_content(
                    result.get("content") if isinstance(result, dict) else result
                ),
                is_error=is_error,
                incident_id=incident_id,
            ))

        async def _on_message(text: str) -> None:
            await self._emit_token(agent_name, text, incident_id=incident_id)
            await self.event_bus.put(AgentMessage(agent=agent_name, text=text, incident_id=incident_id))

        try:
            final_text = await run_tool_loop(
                client=self._llm,
                system_prompt=system,
                user_prompt=user_prompt,
                tools=tool_fns,
                on_tool_call=_on_tool_call,
                on_tool_result=_on_tool_result,
                on_message=_on_message,
                max_turns=self._max_turns,
            )
        except Exception as exc:  # noqa: BLE001
            if os.environ.get("SRE_SWARM_DEBUG"):
                sys.stderr.write(f"[debug] agent '{agent_name}' raised: {exc!r}\n")
            await self._emit(agent_name, "error", str(exc), incident_id=incident_id)
            return _stub_response(agent_name)

        if not final_text:
            if os.environ.get("SRE_SWARM_DEBUG"):
                sys.stderr.write(
                    f"[debug] agent '{agent_name}' produced no final text; using stub\n"
                )
            return _stub_response(agent_name)
        return final_text

    async def _emit(self, agent: str, phase: str, detail=None, incident_id: Optional[str] = None) -> None:
        await self.event_bus.put(AgentPhaseChange(agent=agent, phase=phase, detail=detail, incident_id=incident_id))

    async def _emit_token(self, agent: str, token: str, incident_id: Optional[str] = None) -> None:
        await self.event_bus.put(AgentToken(agent=agent, token=token, incident_id=incident_id))

    async def _emit_done(self, incident_id: str) -> None:
        await self.event_bus.put(PipelineDone(incident_id=incident_id))


def _serialize_tool_content(content: Any) -> Any:
    """Best-effort JSON-friendly representation of a tool result payload."""
    if content is None or isinstance(content, (str, int, float, bool)):
        return content
    if isinstance(content, list):
        out = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text" and "text" in item:
                    out.append(item["text"])
                else:
                    out.append(item)
            else:
                text = getattr(item, "text", None)
                out.append(text if text is not None else repr(item))
        if len(out) == 1:
            return out[0]
        return out
    if isinstance(content, dict):
        return content
    return repr(content)


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _replay_skipped(replay) -> bool:
    """True if the chaos_replay agent returned a no-action verdict."""
    if replay is None:
        return True
    scenario = (getattr(replay, "scenario_applied", "") or "").strip().lower()
    return scenario in {"", "none", "no-scenario", "no_scenario", "n/a"}


def _parse_json(raw: str, model):
    """Best-effort JSON extraction tolerant to stray prose or code fences."""
    if not raw:
        return None
    candidates: list[str] = [raw]
    fence = _FENCE_BLOCK.search(raw)
    if fence:
        candidates.append(fence.group(1))
    match = _JSON_BLOCK.search(raw)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        try:
            return model.model_validate(data)
        except Exception:  # noqa: BLE001
            continue
    if os.environ.get("SRE_SWARM_DEBUG"):
        sys.stderr.write(
            f"[debug] _parse_json failed for {model.__name__}; raw payload:\n{raw}\n"
        )
    return None


def _rca_confirm_summary(incident: Incident) -> str:
    """Single human checkpoint: 'is this the right root cause to act on?'.
    Pulls language straight from the RCA agent's structured output so the
    operator sees the reasoner's actual finding, not a templated string."""
    service = (incident.event.service if incident.event else None) or "the affected service"
    if not incident.rca or not incident.rca.hypotheses:
        return f"No RCA hypothesis available for {service} — proceed anyway?"
    top = incident.rca.hypotheses[0]
    cause = (top.cause or "").strip() or incident.rca.top_cause or "the leading hypothesis"
    conf = int(round((top.confidence or 0.0) * 100))
    ev = (top.evidence or [])
    ev_line = (
        f" Evidence: {ev[0]}" + (f" (+{len(ev) - 1} more)" if len(ev) > 1 else "")
        if ev else ""
    )
    return (
        f"Confirm root cause for {service}: \u201c{cause}\u201d "
        f"(confidence {conf}%).{ev_line} "
        f"Approval authorizes sandbox replay AND live heal-execute."
    )


def _heal_summary(incident: Incident, plan: RemediationPlan) -> str:
    """Derive the heal HITL summary from plan + RCA content."""
    n = len(plan.steps or [])
    writes = sum(1 for s in plan.steps or [] if str(s.blast_radius.value if hasattr(s.blast_radius, "value") else s.blast_radius) != "read_only")
    cause = (incident.rca.top_cause if incident.rca and incident.rca.top_cause else None) or "the incident"
    eta = plan.estimated_recovery_min
    return (
        f"{n} step(s) ({writes} write) to address: {cause}. "
        f"Estimated recovery ~{eta} min."
    )


def _heal_execute_summary(executed: list[dict]) -> str:
    """Human-readable wrap-up so the operator can read the heal outcome at a glance."""
    if not executed:
        return "heal_execute: no steps ran."
    total = len(executed)
    ok = sum(1 for e in executed if e.get("executed"))
    failed = total - ok
    lines = [f"heal_execute: {ok}/{total} step(s) succeeded" + (f", {failed} failed" if failed else "") + "."]
    for e in executed:
        marker = "OK " if e.get("executed") else "X  "
        action = e.get("action") or f"step {e.get('step_number')}"
        suffix = ""
        if e.get("via") == "fallback":
            suffix = " (fallback)"
        if not e.get("executed"):
            msg = e.get("result") or e.get("error") or ""
            if msg:
                suffix += f" — {msg[:140]}"
        lines.append(f"  {marker}#{e.get('step_number')} {action}{suffix}")
    return "\n".join(lines)


_SERVICE_TOKEN_RE = re.compile(r"\b([a-z0-9][a-z0-9-]*-service)\b", re.IGNORECASE)


def _enforce_plan_grounding(plan: RemediationPlan, incident: Incident, default_namespace: str) -> RemediationPlan:
    """Rewrite kubectl_command / rollback_cmd that target the wrong service or
    a missing/wrong namespace. The LLM occasionally hallucinates an unrelated
    service even when the prompt names the right one; we patch those up here so
    the operator never sees a heal plan pointed at the wrong deployment."""
    target_service = (incident.event.service if incident.event else None)
    namespace = (incident.event.namespace if incident.event else None) or default_namespace
    if not target_service or not plan.steps:
        return plan
    for step in plan.steps:
        for attr in ("kubectl_command", "rollback_cmd"):
            cmd = getattr(step, attr, None)
            if not cmd:
                continue
            fixed = _SERVICE_TOKEN_RE.sub(
                lambda m: target_service if m.group(0).lower() != target_service.lower() else m.group(0),
                cmd,
            )
            # Ensure -n <namespace> is present and points at the right ns.
            if "-n " not in fixed and "--namespace" not in fixed and fixed.lstrip().startswith("kubectl"):
                fixed = f"{fixed.rstrip()} -n {namespace}"
            elif "-n default" in fixed and namespace != "default":
                fixed = fixed.replace("-n default", f"-n {namespace}")
            setattr(step, attr, fixed)
    return plan


def _stub_response(agent_name: str) -> str:
    """Deterministic placeholder so the pipeline still produces structured output
    when the LLM backend is unreachable or returns no parsable answer."""
    now = datetime.now(timezone.utc).isoformat()
    if agent_name == "triage":
        return json.dumps(
            {
                "category": "error_rate",
                "affected_services": ["unknown-service"],
                "affected_pods": [],
                "upstream_degraded": False,
                "downstream_degraded": False,
                "anomaly_started_at": now,
                "urgency": "medium",
                "confidence": 0.1,
                "evidence": ["stub response — Ollama returned no parsable result"],
            }
        )
    if agent_name == "rca":
        return json.dumps(
            {
                "hypotheses": [
                    {
                        "cause": "stub — LLM unavailable",
                        "evidence": ["ollama unreachable or empty response", "stub orchestrator path"],
                        "confidence": 0.1,
                    }
                ],
                "top_cause": "stub — LLM unavailable",
                "timeline": [],
                "affected_pods": [],
                "log_patterns": [],
                "resource_issues": False,
            }
        )
    if agent_name == "chaos_replay":
        return json.dumps(
            {
                "scenario_applied": "none",
                "sandbox_namespace": "sre-sandbox",
                "duration_s": 0,
                "validation": "partial",
                "similarity_score": 0.0,
                "sandbox_metrics": {},
                "original_metrics": {},
            }
        )
    if agent_name == "predict":
        return json.dumps(
            {
                "will_self_heal": False,
                "self_heal_reason": None,
                "cascade_risk": "medium",
                "mttr_without_min": 30,
                "risk_level": "medium",
            }
        )
    if agent_name == "heal":
        return json.dumps(
            {
                "incident_id": "STUB",
                "steps": [],
                "estimated_recovery_min": 0,
                "confidence": 0.0,
                "requires_individual_confirm": [],
            }
        )
    return "{}"
