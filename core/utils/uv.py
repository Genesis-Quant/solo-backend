import os
from pathlib import Path

from config import SoloSettings


def uv_environment(directory: Path) -> dict[str, str]:
    """Keep uv's hardlink cache on the target environment's lexical volume."""
    directory = Path(os.path.abspath(directory))
    shared = Path(os.path.abspath(SoloSettings.SHARED_DIR))
    cache = directory.parent / ".uv-cache"
    for group in ("projects", "runs", "artifacts"):
        root = shared / group
        if directory.is_relative_to(root):
            cache = root / ".uv-cache"
            break
    return {"UV_CACHE_DIR": str(cache), "UV_LINK_MODE": "hardlink"}
