from __future__ import annotations

import hashlib
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


def portal_build_id() -> str:
    """Identify the exact application code and portal assets in this checkout.

    The identifier contains no configuration or secret.  It lets local QA and
    production confirm that both are running the same routes, role logic and UI.
    """
    digest = hashlib.sha256()
    paths = sorted(
        path
        for path in APP_DIR.rglob("*")
        if path.is_file() and path.suffix in {".py", ".html", ".css", ".js"}
    )
    for path in paths:
        digest.update(path.relative_to(APP_DIR).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]
