"""Load YAML configs and environment variables."""
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"

load_dotenv(ROOT / ".env")


def _load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_all() -> dict:
    """Return {settings, sources, weights} with org name substituted into URLs."""
    settings = _load_yaml("settings.yaml")
    sources = _load_yaml("sources.yaml")
    weights = _load_yaml("weights.yaml")

    org_name = settings["org"]["name"].replace(" ", "%20")
    for area_sources in sources.values():
        for s in area_sources:
            s["url"] = s["url"].replace("ORG_NAME", org_name)

    DATA_DIR.mkdir(exist_ok=True)
    return {"settings": settings, "sources": sources, "weights": weights}


def env(key: str, required: bool = True) -> str:
    # .strip() guards against secrets pasted with a trailing newline/space — a
    # common GitHub-secrets mistake that makes an API key an illegal HTTP header.
    val = os.environ.get(key, "").strip()
    if required and not val:
        raise SystemExit(f"Missing required environment variable: {key} (see .env.example)")
    return val
