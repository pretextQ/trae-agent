"""Service entry point: uvicorn with exactly one worker per protocol store."""

from __future__ import annotations

import os


def main() -> None:
    import uvicorn

    host = os.environ.get("AGENT_HTTP_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_HTTP_PORT", "19070"))
    uvicorn.run(
        "trae_agent.http_service:app",
        factory=True,
        host=host,
        port=port,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
