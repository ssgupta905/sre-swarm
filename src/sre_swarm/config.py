from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class ClusterConfig(BaseModel):
    context: str = "minikube"
    namespace: str = "sre-demo"
    sandbox_namespace: str = "sre-sandbox"


class PrometheusConfig(BaseModel):
    url: str = "http://localhost:9090"
    timeout_s: int = 10


class LokiConfig(BaseModel):
    url: Optional[str] = None
    timeout_s: int = 10


class SplunkConfig(BaseModel):
    url: str = "http://127.0.0.1:8088"
    token: str = "demo-hec-token"
    timeout_s: int = 5


class GitHubConfig(BaseModel):
    token: str = ""               # Personal access token (classic or fine-grained)
    api_url: str = "https://api.github.com"
    timeout_s: int = 10


class CodeConfig(BaseModel):
    """Local checkout of the application repo used by code_search/git_blame.

    Leave repo_root empty to disable code tools — they will return a "no source
    repo configured" note instead of erroring, so the agent degrades gracefully.
    """
    repo_root: str = ""


class ServiceConfig(BaseModel):
    name: str
    port: int
    chaos_api: Optional[str] = None
    db_api: Optional[str] = None  # base URL of /db/* introspection endpoints


class ThresholdConfig(BaseModel):
    error_rate_pct: float = 5.0
    p99_latency_ms: int = 500
    pod_restarts_5m: int = 3
    db_connections: int = 2
    consecutive_polls: int = 2


class ObserverConfig(BaseModel):
    interval_s: int = 10


class OllamaConfig(BaseModel):
    url: str = "http://localhost:11434"
    model: str = "llama3.1"
    timeout_s: int = 180
    max_turns: int = 12
    keep_alive: str = "10m"


class AgentConfig(BaseModel):
    provider: str = "ollama"
    model: str = "llama3.1"
    max_tokens: int = 4096
    timeout_s: int = 180


class Config(BaseModel):
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)
    loki: LokiConfig = Field(default_factory=LokiConfig)
    splunk: SplunkConfig = Field(default_factory=SplunkConfig)
    github: GitHubConfig = Field(default_factory=GitHubConfig)
    code: CodeConfig = Field(default_factory=CodeConfig)
    services: list[ServiceConfig] = Field(default_factory=list)
    thresholds: ThresholdConfig = Field(default_factory=ThresholdConfig)
    observer: ObserverConfig = Field(default_factory=ObserverConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    auto_heal: bool = False
    audit_log: str = "~/.sre-swarm/audit.log"
    db_path: str = "~/.sre-swarm/incidents.db"

    @property
    def audit_log_path(self) -> Path:
        return Path(os.path.expanduser(self.audit_log))

    @property
    def db_file(self) -> Path:
        return Path(os.path.expanduser(self.db_path))


DEFAULT_CONFIG_PATH = Path("~/.sre-swarm/config.yaml").expanduser()


def load_config(path: Optional[Path] = None) -> Config:
    """Load configuration from YAML, falling back to defaults if missing."""
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    if not target.exists():
        return Config()
    with target.open("r", encoding="utf-8") as fp:
        raw = yaml.safe_load(fp) or {}
    return Config.model_validate(raw)


def save_config(config: Config, path: Optional[Path] = None) -> Path:
    """Persist config to YAML, creating parent directories as needed."""
    target = Path(path) if path else DEFAULT_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fp:
        yaml.safe_dump(config.model_dump(mode="json"), fp, sort_keys=False)
    return target
