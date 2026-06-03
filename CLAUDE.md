# SRE Swarm — Agent Context

## Monitored microservices (demo)
The four services below are scaffolded in a sibling public repo
**https://github.com/ssgupta905/sre-swarm-demo-apps** — FastAPI apps
with realistic Prometheus metrics, dependency graph, chaos endpoints,
and a `docker-compose.yml` that brings up Prometheus (9090) and
Grafana (3000) for end-to-end demos. Run `make up` then `make traffic`
in that repo before starting the swarm.

## Cluster
- Primary namespace:  sre-demo
- Sandbox namespace:  sre-sandbox
- Kubernetes context: minikube

## Services and Ports (inside cluster)
- orders-service        svc/orders-service:80    pods label: app=orders-service
- inventory-service     svc/inventory-service:80 pods label: app=inventory-service
- payments-service      svc/payments-service:80  pods label: app=payments-service
- notifications-service svc/notifications-service:80

## Prometheus Metrics (all services)
- {service}_requests_total{method, endpoint, status}      ← request counter
- {service}_request_duration_seconds{endpoint}            ← latency histogram
- {service}_error_rate                                    ← injected error rate gauge
- {service}_active_total                                  ← active entity count
- {service}_db_connections                                ← DB pool connections
- {service}_downstream_calls_total{target, status}        ← upstream call counter

Replace {service} with: orders | inventory | payments | notifications

## Alert Thresholds
- error_rate_pct:     > 5.0%
- p99_latency_ms:     > 500ms
- pod_restarts_5m:    > 3
- db_connections:     < 2

## Chaos Mesh Scenario Templates
- NetworkChaos/latency:    add 200ms+100ms jitter to orders-service
- PodChaos/pod-kill:       kill one payments-service pod
- StressChaos/cpu:         80% CPU load on inventory-service
- NetworkChaos/partition:  drop TCP between orders and payments
- HTTPChaos/500s:          inject HTTP 500 responses on /api/orders

## Response Format Rules
1. Every agent MUST respond with valid JSON only — no prose, no markdown fences.
2. Confidence values must be floats 0.0–1.0.
3. Timestamps must be ISO 8601 with timezone (UTC preferred).
4. Tool call failures should be noted in the evidence array, not in a separate field.
5. Never guess values — if a tool call is needed to fill a field, make the call.
