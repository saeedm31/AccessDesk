"""Settings and paths. Everything can be overridden with environment variables."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HIGH_RISK_MAX_DAYS = int(os.getenv("HIGH_RISK_MAX_DAYS", "30"))  # privileged access always expires
TOKEN_TTL_S = int(os.getenv("TOKEN_TTL_S", "900"))
MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "6"))
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "6"))  # short-term memory: last N turns verbatim


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("AD_DATA_DIR", ROOT / "data")))
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "anthropic"))
    aws_region: str = field(default_factory=lambda: os.getenv("AWS_REGION", "eu-central-1"))
    agent_model: str = field(default_factory=lambda: os.getenv("AGENT_MODEL", "claude-sonnet-4-6"))

    @property
    def db(self) -> Path:
        return self.data_dir / "company.db"

    @property
    def audit_db(self) -> Path:
        return self.data_dir / "audit.db"

    @property
    def scenarios(self) -> Path:
        return self.data_dir / "scenarios.json"


def secret(data_dir: Path, name: str) -> str:
    """Signing keys: from the environment, or created once per data folder."""
    env = f"{name.upper()}_SECRET"
    if os.getenv(env):
        return os.environ[env]
    path = Path(data_dir) / f".{name}_secret"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32))
        path.chmod(0o600)
    return path.read_text().strip()
