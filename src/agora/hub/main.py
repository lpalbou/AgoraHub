"""CLI entry point: `agora-hub` (or `python -m agora.hub.main`)."""

from __future__ import annotations

import argparse
import os
import secrets


def main() -> None:
    import uvicorn

    from .app import create_app

    parser = argparse.ArgumentParser(description="Run the agora hub")
    parser.add_argument("--host", default=os.environ.get("AGORA_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AGORA_PORT", "8765")))
    parser.add_argument("--db", default=os.environ.get("AGORA_DB", "agora.db"))
    parser.add_argument("--rate-per-minute", type=float,
                        default=float(os.environ.get("AGORA_RATE_PER_MINUTE", "60")))
    args = parser.parse_args()

    admin_key = os.environ.get("AGORA_ADMIN_KEY", "")
    if not admin_key:
        # Generate an ephemeral admin key rather than shipping a default one.
        admin_key = secrets.token_hex(16)
        print(f"AGORA_ADMIN_KEY not set — generated ephemeral admin key: {admin_key}")

    app = create_app(db_path=args.db, admin_key=admin_key, rate_per_minute=args.rate_per_minute)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
