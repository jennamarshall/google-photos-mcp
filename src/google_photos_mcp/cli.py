"""Command line entry point: `google-photos-mcp [serve|auth]`."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="google-photos-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth", help="Sign in to Google and store a token (run once).")
    auth.add_argument("--port", type=int, default=0, help="Loopback port for the OAuth redirect.")

    index = sub.add_parser("index", help="Index a Google Takeout export / photo folder.")
    index.add_argument("folder")

    serve = sub.add_parser("serve", help="Run the MCP server (default).")
    serve.add_argument(
        "--transport", choices=["stdio", "streamable-http"], default="stdio",
        help="stdio for local clients (Claude Desktop / Claude Code); streamable-http to host it.",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)

    if args.command == "auth":
        from .auth import AuthError, run_auth_flow

        try:
            path = run_auth_flow(args.port)
        except AuthError as e:
            sys.exit(str(e))
        print(f"Signed in. Token saved to {path}")
        return

    if args.command == "index":
        from .server import get_library

        print(f"Indexing {args.folder} ...", file=sys.stderr)
        res = get_library().index(args.folder)
        for e in res.errors[:20]:
            print(f"  error: {e}", file=sys.stderr)
        print(
            f"Scanned {res.scanned}: {res.added} added, {res.updated} updated, "
            f"{res.unchanged} unchanged, {res.removed} removed, "
            f"{res.with_sidecar} with Takeout metadata, {len(res.errors)} errors."
        )
        return

    from .server import mcp

    if args.command == "serve" and args.transport == "streamable-http":
        mcp.run("streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()
