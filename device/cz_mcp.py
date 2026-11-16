#!/usr/bin/env python3
"""MCP server (Streamable HTTP, JSON responses) exposing the cz_tools registry.

Standard library only, to stay small on a 512 MB device. Endpoint: POST /mcp
Auth: "Authorization: Bearer <token>", token stored in ~/cz-ai/token (created on first run).
"""
import json
import os
import secrets
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import cz_tools

BASE = Path(__file__).resolve().parent
TOKEN_FILE = BASE / "token"
PORT = int(os.environ.get("CZ_MCP_PORT", "8765"))
VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
INSTRUCTIONS = ("Tools for an M5Stack CardputerZero handheld (320x170 screen, keyboard, stock APPLaunch OS). "
                "Use open_app / go_home to switch apps, press_keys and screenshot to drive menus, and the "
                "system tools for status. UI actions take a few seconds each.")


def token():
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(24))
        TOKEN_FILE.chmod(0o600)
    return TOKEN_FILE.read_text().strip()


def handle(msg):
    """One JSON-RPC message in, one response out (None for notifications)."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    ok = lambda result: {"jsonrpc": "2.0", "id": mid, "result": result}
    err = lambda code, text: {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": text}}
    if method == "initialize":
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in VERSIONS else VERSIONS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": "cardputer-zero", "version": "0.1.0"},
                   "instructions": INSTRUCTIONS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": [{"name": n, "description": t["description"], "inputSchema": t["schema"]}
                             for n, t in cz_tools.TOOLS.items()]})
    if method == "tools/call":
        name = params.get("name")
        if name not in cz_tools.TOOLS:
            return err(-32602, f"unknown tool: {name}")
        try:
            res = cz_tools.call(name, params.get("arguments") or {})
            if isinstance(res, dict) and "_image_png_b64" in res:
                content = [{"type": "image", "data": res["_image_png_b64"], "mimeType": "image/png"}]
            else:
                content = [{"type": "text", "text": json.dumps(res)}]
            return ok({"content": content, "isError": False})
        except Exception as e:
            traceback.print_exc()
            return ok({"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})
    return err(-32601, f"method not found: {method}")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, b'{"ok": true}')
        self._send(405, b"", extra={"Allow": "POST"})

    do_DELETE = do_GET

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
            return self._send(404)
        if self.headers.get("Authorization", "") != f"Bearer {token()}":
            return self._send(401, b'{"error": "unauthorized"}', extra={"WWW-Authenticate": "Bearer"})
        try:
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except Exception:
            return self._send(400, json.dumps({"jsonrpc": "2.0", "id": None,
                                               "error": {"code": -32700, "message": "parse error"}}).encode())
        if isinstance(msg, list):
            out = [r for r in (handle(m) for m in msg) if r is not None]
            return self._send(200, json.dumps(out).encode()) if out else self._send(202)
        out = handle(msg)
        return self._send(202) if out is None else self._send(200, json.dumps(out).encode())

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    token()
    print(f"cz-mcp listening on :{PORT}/mcp with {len(cz_tools.TOOLS)} tools", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
