"""Generate a 'reproduce this incident on minikube' playbook.

The Copilot UI surfaces this to the operator after RCA so they can replay the
failure locally with copy-pasteable commands. We map the incident's `trigger`
(plus, when available, the RCA top hypothesis) to one of the demo-stack chaos
endpoints exposed by every service container.

Output shape is intentionally simple JSON so the front-end can render each
`block` as a copy-button card without needing schema knowledge.
"""
from __future__ import annotations

from typing import Optional

from ..config import Config, ServiceConfig
from ..models import Incident


def _find_service(config: Config, name: str) -> Optional[ServiceConfig]:
    for svc in config.services:
        if svc.name == name:
            return svc
    return None


def _base_url(svc: ServiceConfig) -> str:
    return f"http://127.0.0.1:{svc.port}"


def _prometheus_dashboard_url(prom_url: str, service: str, trigger: str) -> str:
    prefix = service.removesuffix("-service").replace("-", "_")
    queries = {
        "error_rate": f"{prefix}_error_rate",
        "p99_latency": (
            f"histogram_quantile(0.99, sum(rate({prefix}_request_duration_seconds_bucket[5m])) by (le)) * 1000"
        ),
        "pod_restarts": (
            f'sum(increase(kube_pod_container_status_restarts_total{{pod=~"{prefix}.*"}}[5m]))'
        ),
        "db_connections": f"{prefix}_db_connections",
    }
    expr = queries.get(trigger, f"{prefix}_requests_total")
    base = prom_url.rstrip("/").replace("9090", "9090")
    return f"{base}/graph?g0.expr={expr}&g0.tab=0"


def build_replication_recipe(incident: Incident, config: Config) -> dict:
    """Return a UI-friendly recipe of blocks. Always safe to render even if RCA missing."""
    ev = incident.event
    service_name = ev.service if ev else "orders-service"
    trigger = (ev.trigger if ev else "error_rate") or "error_rate"
    namespace = config.cluster.namespace
    svc = _find_service(config, service_name)
    rca_cause = incident.rca.top_cause if incident.rca and incident.rca.top_cause else None
    urgency = incident.urgency.value if incident.urgency else "—"

    summary_parts = [f"{service_name} · {trigger}"]
    if rca_cause:
        summary_parts.append(f"RCA: {rca_cause[:80]}")
    summary = " · ".join(summary_parts)

    blocks: list[dict] = []

    # 1. Prep — point kubectl at the right context
    blocks.append({
        "title": "Prepare minikube",
        "description": "Switch to the local cluster and confirm the demo-stack is healthy before injecting anything.",
        "language": "bash",
        "command": (
            f"kubectl config use-context {config.cluster.context}\n"
            f"kubectl -n {namespace} get pods\n"
            f"kubectl -n {namespace} get svc"
        ),
    })

    # 2. Inject — service-aware chaos action mapped from the breached trigger
    if svc is not None:
        base = _base_url(svc)
        injection = _injection_for_trigger(trigger, base)
        if injection is not None:
            blocks.append(injection)
    else:
        blocks.append({
            "title": "Inject (manual)",
            "description": (
                f"No port configured for {service_name}. Forward the pod and POST to "
                f"/chaos/error_rate (value 0.0–1.0) or /chaos/latency (ms)."
            ),
            "language": "bash",
            "command": (
                f"kubectl -n {namespace} port-forward svc/{service_name} 18080:80 &\n"
                f"curl -X POST 'http://127.0.0.1:18080/chaos/error_rate?value=0.6'"
            ),
        })

    # 3. Observe — Prometheus link + tail logs
    blocks.append({
        "title": "Observe in Prometheus",
        "description": (
            f"The {trigger} signal should breach the configured threshold within "
            f"{config.observer.interval_s * max(2, config.thresholds.consecutive_polls)}s."
        ),
        "language": "bash",
        "command": (
            f"# open in browser:\n"
            f"open '{_prometheus_dashboard_url(config.prometheus.url, service_name, trigger)}'\n\n"
            f"# or tail the pod logs:\n"
            f"kubectl -n {namespace} logs -f deploy/{service_name} --tail=100"
        ),
    })

    # 4. Chaos Mesh variant — sandbox-only, mirrors the chaos_replay agent's path
    blocks.append({
        "title": "Replay via Chaos Mesh (sandbox)",
        "description": (
            f"Apply the equivalent Chaos Mesh CRD to {config.cluster.sandbox_namespace} "
            f"instead of the demo namespace — safer for repeated experimentation."
        ),
        "language": "yaml",
        "command": _chaos_mesh_crd(trigger, service_name, config.cluster.sandbox_namespace),
    })

    # 5. Cleanup — always provide an undo
    if svc is not None:
        cleanup_cmd = (
            f"curl -X POST {_base_url(svc)}/chaos/clear\n"
            f"kubectl -n {namespace} rollout status deploy/{service_name}"
        )
    else:
        cleanup_cmd = (
            f"kubectl -n {config.cluster.sandbox_namespace} delete httpchaos,networkchaos,podchaos,stresschaos --all\n"
            f"kubectl -n {namespace} rollout restart deploy/{service_name}"
        )
    blocks.append({
        "title": "Cleanup",
        "description": "Reset injected chaos so the cluster returns to baseline.",
        "language": "bash",
        "command": cleanup_cmd,
    })

    return {
        "incident_id": incident.id,
        "title": f"Replicate {incident.id} in minikube",
        "summary": summary,
        "urgency": urgency,
        "service": service_name,
        "trigger": trigger,
        "rca_cause": rca_cause,
        "blocks": blocks,
    }


def _injection_for_trigger(trigger: str, base: str) -> Optional[dict]:
    if trigger == "error_rate":
        return {
            "title": "Inject 60% HTTP 500s",
            "description": (
                "Tell the service to return HTTP 500 on 60% of incoming requests. "
                "Crosses the default 5% error threshold in two observer polls."
            ),
            "language": "bash",
            "command": f"curl -X POST '{base}/chaos/error_rate?value=0.6'",
        }
    if trigger == "p99_latency":
        return {
            "title": "Inject 800ms latency",
            "description": "Adds 800ms to every request — p99 should breach the 500ms ceiling.",
            "language": "bash",
            "command": f"curl -X POST '{base}/chaos/latency?ms=800'",
        }
    if trigger == "pod_restarts":
        return {
            "title": "Kill the pod (forces a restart)",
            "description": "Hard-exits the container. Kubernetes will restart it; do this 4× to breach the >3-in-5m rule.",
            "language": "bash",
            "command": f"for i in 1 2 3 4; do curl -X POST {base}/chaos/kill; sleep 5; done",
        }
    if trigger == "db_connections":
        return {
            "title": "Drop DB connections",
            "description": (
                "The shared app does not expose a direct db_connections chaos toggle, but "
                "stressing CPU starves the connection-keeper goroutine and trips the "
                "<2 connections threshold."
            ),
            "language": "bash",
            "command": f"curl -X POST '{base}/chaos/cpu_stress?seconds=120'",
        }
    return {
        "title": "Inject (generic error_rate)",
        "description": "Fallback for manual triggers — 40% HTTP 500s.",
        "language": "bash",
        "command": f"curl -X POST '{base}/chaos/error_rate?value=0.4'",
    }


def _chaos_mesh_crd(trigger: str, service: str, sandbox_ns: str) -> str:
    selector = (
        f"  selector:\n"
        f"    namespaces: [{sandbox_ns}]\n"
        f"    labelSelectors:\n"
        f"      app: {service}\n"
    )
    if trigger == "p99_latency":
        return (
            "apiVersion: chaos-mesh.org/v1alpha1\n"
            "kind: NetworkChaos\n"
            f"metadata: {{ name: replay-{service}-latency, namespace: {sandbox_ns} }}\n"
            "spec:\n"
            "  action: delay\n"
            "  mode: all\n"
            f"{selector}"
            "  delay:\n"
            "    latency: 800ms\n"
            "    jitter: 100ms\n"
            "  duration: 120s\n"
        )
    if trigger == "pod_restarts":
        return (
            "apiVersion: chaos-mesh.org/v1alpha1\n"
            "kind: PodChaos\n"
            f"metadata: {{ name: replay-{service}-kill, namespace: {sandbox_ns} }}\n"
            "spec:\n"
            "  action: pod-kill\n"
            "  mode: one\n"
            f"{selector}"
            "  scheduler: { cron: '@every 30s' }\n"
        )
    if trigger == "db_connections":
        return (
            "apiVersion: chaos-mesh.org/v1alpha1\n"
            "kind: StressChaos\n"
            f"metadata: {{ name: replay-{service}-cpu, namespace: {sandbox_ns} }}\n"
            "spec:\n"
            "  mode: one\n"
            f"{selector}"
            "  stressors:\n"
            "    cpu: { workers: 2, load: 80 }\n"
            "  duration: 120s\n"
        )
    # Default: HTTP 500s
    return (
        "apiVersion: chaos-mesh.org/v1alpha1\n"
        "kind: HTTPChaos\n"
        f"metadata: {{ name: replay-{service}-500s, namespace: {sandbox_ns} }}\n"
        "spec:\n"
        "  mode: all\n"
        f"{selector}"
        "  target: Response\n"
        "  port: 80\n"
        "  abort: false\n"
        "  replace:\n"
        "    code: 500\n"
        "  duration: 120s\n"
    )
