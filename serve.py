"""Run the Stremio addon with Granian (foreground - Ctrl+C stops it).

Usage:
    python serve.py [port] [host]

Defaults: 127.0.0.1:7003  ->  manifest at http://127.0.0.1:7003/manifest.json
"""
from __future__ import annotations

import sys

from granian import Granian
from granian.constants import Interfaces


def main() -> None:
    host = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7003

    server = Granian(
        "app.main:app",
        address=host,
        port=port,
        interface=Interfaces.ASGI,
    )
    print(f"Serving {host}:{port}  (manifest: http://{host}:{port}/manifest.json)")
    server.serve()


if __name__ == "__main__":
    main()
