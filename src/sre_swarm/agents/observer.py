"""Observer agent: continuously polls Prometheus + health + Splunk and emits
IncidentEvents plus a multi-source VulnerabilityReport.

The Observer does NOT run any LLM agent — it only detects threshold breaches
and pushes IncidentEvent messages onto the event bus. Per FR-021 a breach is
confirmed only after `thresholds.consecutive_polls` polls in a row.

Beyond the original Prometheus-only flow, each poll now also probes every
service's /health endpoint and counts recent error log volume in Splunk. All
concerning signals are bundled into a `VulnerabilityReport` event so the UI can
surface the full inventory of vulnerabilities, not just the first Prom breach.
"""
from __future__ import annotations

import asyncio
import itertools
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..config import Config, ServiceConfig
from ..models import IncidentEvent
from ..tools.prometheus import configure_prometheus, prometheus_instant_value
from .events import Finding, VulnerabilityReport


_PROMQL = {
    "error_rate": 'sum(rate({svc}_requests_total{{status=~"5.."}}[1m])) '
                   '/ sum(rate({svc}_requests_total[1m])) * 100',
    "p99_latency": 'histogram_quantile(0.99, '
                    'sum(rate({svc}_request_duration_seconds_bucket[5m])) by (le)) * 1000',
    "pod_restarts": 'sum(increase(kube_pod_container_status_restarts_total'
                     '{{namespace="{ns}",pod=~"{svc}.*"}}[5m]))',
    "db_connections": '{svc}_db_connections',
}


def _metric_prefix(service: str) -> str:
    """Service names like 'orders-service' become 'orders' for PromQL."""
    return service.removesuffix("-service").replace("-", "_")


def _service_base_url(svc: ServiceConfig) -> str:
    return f"http://127.0.0.1:{svc.port}"


class ObserverAgent:
    """Polls Prometheus, /health, and Splunk on an interval."""

    def __init__(
        self,
        config: Config,
        event_bus: asyncio.Queue,
        incident_id_factory=None,
    ) -> None:
        self.config = config
        self.event_bus = event_bus
        self._incident_id_factory = incident_id_factory or self._default_id
        configure_prometheus(config.prometheus.url, config.prometheus.timeout_s)
        self._breach_count: dict[tuple[str, str], int] = defaultdict(int)
        self._heal_count: dict[tuple[str, str], int] = defaultdict(int)
        self._first_seen: dict[tuple[str, str], datetime] = {}
        self._open_incidents: dict[tuple[str, str], IncidentEvent] = {}
        self._last_findings_signature: Optional[tuple] = None
        self._counter = itertools.count(1)
        self._running = False
        self._last_poll: Optional[datetime] = None
        # After a dismiss or pipeline-complete-without-fix, suppress re-firing
        # the same (service, trigger) until this absolute deadline. Keeps the
        # inbox from filling up with N copies of the same anomaly.
        self._cooldowns: dict[tuple[str, str], datetime] = {}
        self._cooldown_s: int = 300  # 5 min default

    @property
    def last_poll(self) -> Optional[datetime]:
        return self._last_poll

    def _default_id(self) -> str:
        return f"INC-{next(self._counter):03d}"

    async def run(self) -> None:
        self._running = True
        interval = max(1, self.config.observer.interval_s)
        try:
            while self._running:
                try:
                    await self.poll_once()
                except Exception as exc:  # noqa: BLE001
                    await self._emit_log(f"observer poll error: {exc}")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            self._running = False
            raise

    async def stop(self) -> None:
        self._running = False

    async def poll_once(self) -> list[IncidentEvent]:
        """Single poll: Prom + health + Splunk for every service. Returns new incidents."""
        self._last_poll = datetime.now(timezone.utc)
        thresholds = self.config.thresholds
        new_events: list[IncidentEvent] = []
        findings: list[Finding] = []
        services = self.config.services or []
        if not services:
            return new_events

        # Run all three sources in parallel across all services.
        prom_task = asyncio.gather(
            *(self._collect_samples(svc.name) for svc in services),
            return_exceptions=True,
        )
        health_task = asyncio.gather(
            *(self._check_health(svc) for svc in services),
            return_exceptions=True,
        )
        splunk_task = asyncio.gather(
            *(self._check_splunk(svc.name) for svc in services),
            return_exceptions=True,
        )
        prom_all, health_all, splunk_all = await asyncio.gather(
            prom_task, health_task, splunk_task
        )

        for svc, samples, health_finding, splunk_finding in zip(
            services, prom_all, health_all, splunk_all
        ):
            if isinstance(samples, Exception):
                samples = {k: None for k in _PROMQL}

            checks = [
                ("error_rate",     samples.get("error_rate"),     thresholds.error_rate_pct, "gt"),
                ("p99_latency",    samples.get("p99_latency"),    thresholds.p99_latency_ms, "gt"),
                ("pod_restarts",   samples.get("pod_restarts"),   thresholds.pod_restarts_5m, "gt"),
                ("db_connections", samples.get("db_connections"), thresholds.db_connections, "lt"),
            ]
            for trigger, value, threshold, op in checks:
                if value is None:
                    continue
                breached = (op == "gt" and value > threshold) or (
                    op == "lt" and value < threshold
                )
                if breached:
                    findings.append(Finding(
                        source="prometheus",
                        service=svc.name,
                        signal=trigger,
                        severity=_severity_for(trigger, value, threshold, op),
                        value=round(value, 3) if isinstance(value, float) else value,
                        threshold=threshold,
                    ))
                event = await self._handle_signal(
                    svc.name, trigger, value, threshold, breached
                )
                if event is not None:
                    new_events.append(event)

            if isinstance(health_finding, Finding):
                findings.append(health_finding)
            if isinstance(splunk_finding, Finding):
                findings.append(splunk_finding)

        # Sort by severity (critical first) so the UI rail puts the worst on top.
        findings.sort(key=lambda f: _SEV_ORDER.get(f.severity, 99))
        # Dedup: only emit when the set of (source, service, signal, severity)
        # changes, otherwise the UI is spammed with identical reports each tick.
        signature = tuple(
            (f.source, f.service, f.signal, f.severity) for f in findings
        )
        if signature != self._last_findings_signature:
            self._last_findings_signature = signature
            await self.event_bus.put(VulnerabilityReport(
                generated_at=self._last_poll.isoformat(),
                findings=findings,
            ))
        return new_events

    async def _collect_samples(self, service: str) -> dict[str, Optional[float]]:
        namespace = self.config.cluster.namespace
        prefix = _metric_prefix(service)
        queries = {
            key: template.format(svc=prefix, ns=namespace)
            for key, template in _PROMQL.items()
        }
        results = await asyncio.gather(
            *(prometheus_instant_value(q) for q in queries.values()),
            return_exceptions=True,
        )
        samples: dict[str, Optional[float]] = {}
        for key, value in zip(queries.keys(), results):
            samples[key] = None if isinstance(value, Exception) else value
        return samples

    async def _check_health(self, svc: ServiceConfig) -> Optional[Finding]:
        url = f"{_service_base_url(svc)}/health"
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(url)
            if r.status_code >= 500:
                return Finding(
                    source="health", service=svc.name, signal="health",
                    severity="critical", value=r.status_code,
                    note=f"{url} returned {r.status_code}",
                )
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            err = float(data.get("injected_error_rate", 0) or 0)
            lat = float(data.get("injected_latency_ms", 0) or 0)
            if err >= 0.5:
                return Finding(
                    source="health", service=svc.name, signal="injected_errors",
                    severity="high", value=err, threshold=0.5,
                    note=f"chaos injection: error_rate={err}",
                )
            if lat >= 500:
                return Finding(
                    source="health", service=svc.name, signal="injected_latency",
                    severity="medium", value=lat, threshold=500,
                    note=f"chaos injection: latency_ms={lat}",
                )
            return None
        except httpx.HTTPError as exc:
            return Finding(
                source="health", service=svc.name, signal="unreachable",
                severity="critical", value=None,
                note=f"{url}: {exc.__class__.__name__}",
            )

    async def _check_splunk(self, service: str) -> Optional[Finding]:
        prefix = _metric_prefix(service)
        url = f"{self.config.splunk.url}/api/search"
        params = {
            "service": prefix,
            "level": "ERROR",
            "minutes": 2,
            "limit": 200,
        }
        try:
            async with httpx.AsyncClient(timeout=self.config.splunk.timeout_s) as c:
                r = await c.get(url, params=params)
            if r.status_code != 200:
                return None
            payload = r.json()
            count = payload.get("count") or len(payload.get("events", []))
            if count >= 20:
                sev = "critical" if count >= 100 else "high" if count >= 50 else "medium"
                return Finding(
                    source="splunk", service=service, signal="error_volume",
                    severity=sev, value=count, threshold=20,
                    note=f"{count} ERROR-level events in last 2m",
                )
            return None
        except httpx.HTTPError:
            return None

    async def _handle_signal(
        self,
        service: str,
        trigger: str,
        value: float,
        threshold: float,
        breached: bool,
    ) -> Optional[IncidentEvent]:
        key = (service, trigger)
        now = datetime.now(timezone.utc)
        if not breached:
            self._breach_count.pop(key, None)
            self._first_seen.pop(key, None)
            if key in self._open_incidents:
                self._heal_count[key] += 1
                if self._heal_count[key] >= self.config.thresholds.consecutive_polls:
                    self._open_incidents.pop(key, None)
                    self._heal_count.pop(key, None)
            return None

        self._heal_count.pop(key, None)

        # Cooldown: if the operator dismissed or the pipeline closed this
        # (service, trigger) recently, stay quiet so the inbox doesn't refill.
        cooldown_until = self._cooldowns.get(key)
        if cooldown_until and now < cooldown_until:
            return None
        if cooldown_until and now >= cooldown_until:
            self._cooldowns.pop(key, None)

        if key not in self._first_seen:
            self._first_seen[key] = now
        self._breach_count[key] += 1

        if key in self._open_incidents:
            existing = self._open_incidents[key]
            existing.value = value
            return None

        if self._breach_count[key] < self.config.thresholds.consecutive_polls:
            return None

        event = IncidentEvent(
            id=self._incident_id_factory(),
            service=service,
            namespace=self.config.cluster.namespace,
            trigger=trigger,
            value=value,
            threshold=threshold,
            first_seen=self._first_seen[key],
            confirmed_at=now,
        )
        self._open_incidents[key] = event
        await self.event_bus.put(event)
        return event

    def close_incident(self, service: str, trigger: str, cooldown_s: Optional[int] = None) -> None:
        """Release the open-incident lock and set a cooldown so the same
        (service, trigger) can't re-fire immediately on the next poll."""
        key = (service, trigger)
        self._open_incidents.pop(key, None)
        self._breach_count.pop(key, None)
        self._first_seen.pop(key, None)
        seconds = cooldown_s if cooldown_s is not None else self._cooldown_s
        if seconds > 0:
            from datetime import timedelta
            self._cooldowns[key] = datetime.now(timezone.utc) + timedelta(seconds=seconds)

    async def _emit_log(self, message: str) -> None:
        try:
            self.event_bus.put_nowait({"type": "observer_log", "message": message})
        except asyncio.QueueFull:  # pragma: no cover
            pass


_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _severity_for(trigger: str, value: float, threshold: float, op: str) -> str:
    """Cheap heuristic: how far past the threshold are we?"""
    try:
        if op == "gt":
            ratio = value / threshold if threshold else 999
        else:  # lt
            ratio = threshold / value if value else 999
    except ZeroDivisionError:
        ratio = 999
    if ratio >= 4:   return "critical"
    if ratio >= 2:   return "high"
    if ratio >= 1.2: return "medium"
    return "low"


def new_incident_id() -> str:
    """Stable but unique fallback ID for ad-hoc incidents."""
    return f"INC-{uuid.uuid4().hex[:6].upper()}"
