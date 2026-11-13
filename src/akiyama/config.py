"""Settings loading. Secrets come only from the environment (or a local .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS = REPO_ROOT / "config" / "settings.yaml"


@dataclass
class Settings:
    data_dir: Path
    base_url: str = "https://api.jquants.com/v2"
    min_interval_sec: float = 0.6
    max_retries: int = 5
    default_datasets: list[str] = field(default_factory=list)
    availability: dict[str, str] = field(default_factory=dict)
    td_doc_types: list[str] = field(default_factory=lambda: ["pdf", "summaryPdf"])

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def norm_dir(self) -> Path:
        return self.data_dir / "norm"


def load_settings(path: Path | None = None, data_dir: Path | None = None) -> Settings:
    path = path or Path(os.environ.get("AKIYAMA_SETTINGS", DEFAULT_SETTINGS))
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    jq = raw.get("jquants", {})
    dd = Path(data_dir or os.environ.get("AKIYAMA_DATA_DIR") or raw.get("data_dir", "data"))
    if not dd.is_absolute():
        dd = REPO_ROOT / dd
    return Settings(
        data_dir=dd,
        base_url=jq.get("base_url", "https://api.jquants.com/v2"),
        min_interval_sec=float(jq.get("min_interval_sec", 0.6)),
        max_retries=int(jq.get("max_retries", 5)),
        default_datasets=list(raw.get("default_datasets", [])),
        availability=dict(raw.get("availability", {})),
        td_doc_types=list(raw.get("td_docs", {}).get("types", ["pdf", "summaryPdf"])),
    )


def api_key_from_env() -> str:
    """Read JQUANTS_API_KEY from the environment or a .env at the repo root."""
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env", override=False)
    except ImportError:  # pragma: no cover
        pass
    key = os.environ.get("JQUANTS_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "JQUANTS_API_KEY is not set. Put it in .env (see .env.example) or the environment."
        )
    return key
