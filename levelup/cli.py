"""Installed web-server entry point; assets resolve inside the package."""
import argparse
import os


def main():
    parser = argparse.ArgumentParser(description="Run the Levelup web game (one worker).")
    parser.add_argument("--host", default=os.environ.get("LEVELUP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=os.environ.get("LEVELUP_PORT", "8765"))
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    import uvicorn
    uvicorn.run("levelup.server:app", host=args.host, port=args.port,
                workers=1, ws_max_size=16384)
