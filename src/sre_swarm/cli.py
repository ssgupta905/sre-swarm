"""Click entry points. Phase 1 wires up `investigate` end-to-end; the other
commands are stubbed and will be filled in by later phases (B.13)."""
from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

from .agents.heal import ApprovalDecision
from .agents.observer import new_incident_id
from .agents.orchestrator import SwarmOrchestrator
from .audit import AuditLog
from .config import DEFAULT_CONFIG_PATH, Config, load_config, save_config
from .models import BlastRadius, Incident, IncidentEvent, RemediationPlan


_INC_PATTERN = re.compile(r"^INC-[A-Z0-9]+$", re.IGNORECASE)


@click.group(help="SRE Swarm CLI — agentic Kubernetes incident response.")
@click.version_option(package_name="sre-swarm")
def main() -> None:
    """Root command group."""


@main.command("investigate")
@click.argument("target", required=True)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
    help=f"Path to config file (default: {DEFAULT_CONFIG_PATH})",
)
@click.option(
    "--namespace",
    default=None,
    help="Override cluster.namespace from config.",
)
@click.option(
    "--interactive/--no-interactive",
    default=False,
    help="Prompt for HITL approval of the remediation plan (default: off).",
)
def investigate_cmd(
    target: str,
    config_path: Optional[Path],
    namespace: Optional[str],
    interactive: bool,
) -> None:
    """Run the swarm pipeline on TARGET (an incident ID or free-text description)."""
    config = load_config(config_path)
    if namespace:
        config.cluster.namespace = namespace

    incident, description = _make_incident_from_target(target, config)
    audit = AuditLog(config.audit_log_path)
    hitl_callback = _cli_hitl(incident.id, audit) if interactive else None
    orchestrator = SwarmOrchestrator(config, hitl_callback=hitl_callback)
    enriched = asyncio.run(orchestrator.run(incident, description=description))
    click.echo(json.dumps(enriched.model_dump(mode="json"), indent=2, default=str))


@main.command("monitor")
@click.option("--namespace", default=None, help="Kubernetes namespace to watch.")
@click.option("--interval", type=int, default=None, help="Observer poll interval seconds.")
@click.option("--auto-heal", is_flag=True, default=False, help="Enable auto-heal mode (skips HITL).")
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
)
def monitor_cmd(namespace, interval, auto_heal, config_path) -> None:
    """Launch the interactive TUI (FR-001). Wired in Phase 3."""
    config = load_config(config_path)
    if namespace:
        config.cluster.namespace = namespace
    if interval:
        config.observer.interval_s = interval
    if auto_heal:
        config.auto_heal = True
    try:
        from .tui.app import SRESwarmApp
    except ImportError:
        click.echo("TUI not available in this build (Phase 3 not yet implemented).", err=True)
        sys.exit(2)
    SRESwarmApp(config).run()


@main.command("web")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8080, type=int, show_default=True)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path),
    default=None,
)
def web_cmd(host: str, port: int, config_path: Optional[Path]) -> None:
    """Launch the browser UI (FastAPI + WebSocket on http://HOST:PORT)."""
    config = load_config(config_path)
    try:
        from .web.server import run
    except ImportError as e:
        click.echo(f"Web UI dependencies missing: {e}", err=True)
        click.echo("Install with: pip install fastapi uvicorn", err=True)
        sys.exit(2)
    click.echo(f"SRE Swarm web UI starting at http://{host}:{port}")
    run(config, host=host, port=port)


@main.command("replay")
@click.argument("scenario", type=click.Choice(
    ["latency", "pod-kill", "cpu-stress", "network-partition", "payment-down"]
))
def replay_cmd(scenario: str) -> None:
    """Inject a Chaos Mesh scenario and run the swarm pipeline (FR-003)."""
    click.echo(f"replay '{scenario}' not yet implemented (Phase 2).", err=True)
    sys.exit(2)


@main.command("status")
def status_cmd() -> None:
    """Print current system status (FR-004)."""
    click.echo("status not yet implemented (Phase 5).", err=True)
    sys.exit(2)


@main.command("incidents")
@click.option("--open", "show_open", is_flag=True, default=False)
@click.option("--resolved", "show_resolved", is_flag=True, default=False)
@click.option("--limit", type=int, default=50)
def incidents_cmd(show_open: bool, show_resolved: bool, limit: int) -> None:
    """List local incidents (FR-005)."""
    click.echo("incidents not yet implemented (Phase 5).", err=True)
    sys.exit(2)


@main.command("export")
@click.argument("incident_id")
@click.option("--output", type=click.Path(path_type=Path), default=None)
def export_cmd(incident_id: str, output: Optional[Path]) -> None:
    """Export an incident report (FR-006)."""
    click.echo("export not yet implemented (Phase 5).", err=True)
    sys.exit(2)


@main.group("config")
def config_group() -> None:
    """Config management commands."""


@config_group.command("init")
def config_init_cmd() -> None:
    """Interactive wizard to create config.yaml (FR-007)."""
    cfg = Config()
    cfg.prometheus.url = click.prompt(
        "Prometheus URL", default=cfg.prometheus.url, show_default=True
    )
    cfg.ollama.url = click.prompt(
        "Ollama URL", default=cfg.ollama.url, show_default=True
    )
    cfg.ollama.model = click.prompt(
        "Ollama model (must support tool calling, e.g. llama3.1, qwen2.5)",
        default=cfg.ollama.model,
        show_default=True,
    )
    cfg.agent.model = cfg.ollama.model
    cfg.cluster.context = click.prompt(
        "kubectl context", default=cfg.cluster.context, show_default=True
    )
    cfg.cluster.namespace = click.prompt(
        "Primary namespace", default=cfg.cluster.namespace, show_default=True
    )
    cfg.thresholds.error_rate_pct = click.prompt(
        "Error rate threshold (%)",
        default=cfg.thresholds.error_rate_pct,
        type=float,
        show_default=True,
    )
    cfg.thresholds.p99_latency_ms = click.prompt(
        "P99 latency threshold (ms)",
        default=cfg.thresholds.p99_latency_ms,
        type=int,
        show_default=True,
    )
    path = save_config(cfg)
    click.echo(f"wrote config to {path}")


def _make_incident_from_target(target: str, config: Config) -> tuple[Incident, Optional[str]]:
    """Build an Incident shell from either an INC-ID or a free-text description."""
    now = datetime.now(timezone.utc)
    if _INC_PATTERN.match(target):
        # Phase 1 doesn't yet load incidents from the DB — synthesise a minimal one.
        event = IncidentEvent(
            id=target,
            service=(config.services[0].name if config.services else "unknown"),
            namespace=config.cluster.namespace,
            trigger="manual",
            value=0.0,
            threshold=0.0,
            first_seen=now,
            confirmed_at=now,
        )
        incident = Incident(id=target, event=event, created_at=now, updated_at=now)
        return incident, None

    description = target
    incident_id = new_incident_id()
    service = _guess_service_from_text(description, config) or "unknown"
    event = IncidentEvent(
        id=incident_id,
        service=service,
        namespace=config.cluster.namespace,
        trigger="manual",
        value=0.0,
        threshold=0.0,
        first_seen=now,
        confirmed_at=now,
    )
    incident = Incident(id=incident_id, event=event, created_at=now, updated_at=now)
    return incident, description


def _cli_hitl(incident_id: str, audit: AuditLog):
    """Build an HITL callback that prompts the operator on the terminal.

    Honors FR-041: HIGH/CRITICAL blast-radius steps need individual confirmation
    via typing the word 'confirm'. Approval decisions are written to the audit log
    but the actual write tools are NOT executed in this Phase 4 CLI path —
    execution lands with the TUI in Phase 3+, or future versions.
    """
    async def _callback(plan: RemediationPlan):
        click.echo("\n=== HITL approval required ===")
        click.echo(f"Incident: {incident_id}  |  Plan steps: {len(plan.steps)}  |  "
                   f"Recovery est: {plan.estimated_recovery_min}m")
        for step in plan.steps:
            click.echo(
                f"  [{step.step_number}] blast={step.blast_radius.value:<9} "
                f"{step.action}"
            )
        choice = click.prompt(
            "Action (a=approve all, r=reject, s=skip)",
            default="s",
            show_default=True,
        ).strip().lower()
        if choice == "r":
            reason = click.prompt("Rejection reason", default="not specified")
            audit.write("hitl_rejected", incident=incident_id, reason=reason)
            return ApprovalDecision(rejected=True, rejection_reason=reason)
        if choice != "a":
            audit.write("hitl_skipped", incident=incident_id)
            return ApprovalDecision()
        approved: list[int] = []
        for step in plan.steps:
            if step.blast_radius in (BlastRadius.HIGH, BlastRadius.CRITICAL):
                typed = click.prompt(
                    f"Step {step.step_number} has {step.blast_radius.value.upper()} blast radius "
                    f"({step.action}) — type 'confirm' to approve",
                    default="skip",
                )
                if typed.strip().lower() != "confirm":
                    audit.write(
                        "hitl_skipped_step",
                        incident=incident_id,
                        step=step.step_number,
                        reason="high-blast confirmation declined",
                    )
                    continue
            approved.append(step.step_number)
        audit.write(
            "hitl_approval",
            incident=incident_id,
            decision="approve_all",
            steps=approved,
        )
        return ApprovalDecision(approved_steps=approved)

    return _callback


def _guess_service_from_text(text: str, config: Config) -> Optional[str]:
    lower = text.lower()
    for svc in config.services:
        if svc.name.lower() in lower:
            return svc.name
    return None


if __name__ == "__main__":
    main()
