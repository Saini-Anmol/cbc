"""
JK Tyre BTP — shared environment / DB config for the CBC pipeline.

Reads credentials from a local `.env` (key=value lines) so they live in ONE
place instead of being hardcoded in curing_lp.py / building.py / cbc.py.

.env keys (see .env in this folder):
    JKT_DB_HOST, JKT_DB_PORT, JKT_DB_USER, JKT_DB_PASSWORD, JKT_DB_DATABASE
    MES_API_KEY   (used by data_fetch.py)

Secrets are NEVER hardcoded here — they must be provided via .env or the process
environment. Missing required keys raise at access time so misconfiguration
fails loudly instead of silently using a stale credential.
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")

# Project data layout (inputs the user drops in, outputs we write).
INPUT_DIR = os.path.join(HERE, "data", "input")
OUTPUT_DIR = os.path.join(HERE, "data", "output")

# Non-secret defaults only. Host/user/password/database must come from .env or
# the process environment — there are intentionally no credential fallbacks.
_DEFAULTS = {
    "JKT_DB_PORT": "3306",
    "JKT_DB_DATABASE": "jkplanningV1",
}

# Keys that must be present (no default) — accessing them when unset raises.
_REQUIRED = ("JKT_DB_HOST", "JKT_DB_USER", "JKT_DB_PASSWORD")


def _load_env_file(path: str = ENV_PATH) -> dict:
    vals = dict(_DEFAULTS)
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    # process-env overrides file, file overrides fallback
    for k in list(vals):
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


ENV = _load_env_file()


def require(key: str) -> str:
    """Return a required env value, raising a clear error if it is unset."""
    val = ENV.get(key)
    if not val:
        raise RuntimeError(
            f"Missing required config '{key}'. Set it in {ENV_PATH} "
            f"(key=value) or as an environment variable.")
    return val


def db_config() -> dict:
    """Return {host, port, user, password, database} for the planning DB."""
    for k in _REQUIRED:
        require(k)
    return {
        "host": ENV["JKT_DB_HOST"],
        "port": int(ENV.get("JKT_DB_PORT", 3306)),
        "user": ENV["JKT_DB_USER"],
        "password": ENV["JKT_DB_PASSWORD"],
        "database": ENV["JKT_DB_DATABASE"],
    }


def mes_api_key() -> str:
    """MES export API key for data_fetch.py — required, no fallback."""
    return require("MES_API_KEY")


def db_url() -> str:
    """SQLAlchemy URL for mysql+pymysql."""
    c = db_config()
    return (f"mysql+pymysql://{c['user']}:{c['password']}"
            f"@{c['host']}:{c['port']}/{c['database']}")


def make_engine(connect_timeout: int = 15):
    from sqlalchemy import create_engine
    # pool_pre_ping  → test (and transparently replace) a connection before use,
    #                  so a dropped/stale connection reconnects instead of raising
    #                  "Can't reconnect until invalid transaction is rolled back".
    # pool_recycle   → recycle connections older than this (sec) before the
    #                  remote MySQL's idle wait_timeout can kill them — important
    #                  because the engine sits idle through the long build phase.
    return create_engine(
        db_url(),
        connect_args={"connect_timeout": connect_timeout},
        pool_pre_ping=True,
        pool_recycle=280,
    )


def in_path(name: str) -> str:
    return os.path.join(INPUT_DIR, name)


def out_path(name: str) -> str:
    return os.path.join(OUTPUT_DIR, name)
