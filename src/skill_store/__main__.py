"""The stdio entry point. The gateway owns authentication and HTTP."""

import os
import sys
from pathlib import Path

from .server import create_server


def main() -> None:
    state = os.environ.get("SKILLS_STATE_DIR", "")
    if not state:
        print("SKILLS_STATE_DIR must name an existing writable directory.", file=sys.stderr)
        raise SystemExit(2)
    server = create_server(
        Path(state),
        namespace=os.environ.get("SKILLS_NAMESPACE", "skills"),
        prompt_separator=os.environ.get("SKILLS_PROMPT_SEPARATOR", "/"),
    )
    server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
