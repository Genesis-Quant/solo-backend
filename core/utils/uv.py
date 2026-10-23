import os
from pathlib import Path


def uv_environment(directory: Path) -> dict[str, str]:
    """Use this service's uv cache configuration, or uv's default cache directory."""
    environment = {"UV_LINK_MODE": os.environ.get("UV_LINK_MODE") or "hardlink"}
    if "UV_CACHE_DIR" in os.environ:
        environment["UV_CACHE_DIR"] = os.environ["UV_CACHE_DIR"]
    return environment
