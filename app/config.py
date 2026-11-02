"""Configuration: environment variables + config/*.yaml."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
CORPUS_DIR = ROOT / "corpus"
UPLOAD_PREFIX = "upload-"     # opportunity ids of uploaded TORs; their results never go to samples/ (tracked, shipped)
SAMPLES_DIR = ROOT / "samples"
CACHE_DIR = ROOT / ".cache"


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f)


@lru_cache(maxsize=1)
def estimation_config() -> dict:
    return load_yaml("estimation.yaml")


@lru_cache(maxsize=1)
def pricing_config() -> dict:
    return load_yaml("pricing.yaml")


@lru_cache(maxsize=1)
def pipeline_config() -> dict:
    return load_yaml("pipeline.yaml")


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def load_dotenv_if_present() -> None:
    path = ROOT / ".env"
    if path.exists():
        from dotenv import load_dotenv
        load_dotenv(path, override=False)
