"""A minimal fake of the Omada Open API endpoints the monitor uses, for the
container smoke test.

Serves two clients over plain HTTP: an IoT client (VLAN 20) uploading at a
steady 50 Mbps, enough to trip thresholds.example.json's sustained upload
limit, and an idle client on the default VLAN.

Also fakes a Discord webhook at /webhook: the first post gets a 429 with
retry_after, and later ones are recorded. GET /webhook/posts returns them.

Usage: python3 fake_omada.py <port>
"""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "smoke-test-token"
UPLOAD_BYTES_PER_SECOND = 50_000_000 // 8
STARTED = time.time()
WEBHOOK = {"rate_limited": False, "posts": []}


def clients() -> list[dict]:
    elapsed = time.time() - STARTED
    return [
        {
            "mac": "02-00-00-00-00-01", "name": "Smoke Camera", "ip": "10.0.20.10", "vid": 20,
            "trafficUp": int(elapsed * UPLOAD_BYTES_PER_SECOND), "trafficDown": int(elapsed * 1000),
            "uptime": int(elapsed) + 1000,
        },
        {
            "mac": "02-00-00-00-00-02", "name": "Smoke Laptop", "ip": "10.0.0.10", "vid": 0,
            "trafficUp": 0, "trafficDown": 0, "uptime": int(elapsed) + 1000,
        },
    ]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/webhook":
            content = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["content"]
            if not WEBHOOK["rate_limited"]:
                WEBHOOK["rate_limited"] = True
                self.reply({"message": "You are being rate limited.", "retry_after": 0.2}, status=429)
            else:
                WEBHOOK["posts"].append(content)
                self.reply({}, status=200)
        elif self.path.startswith("/openapi/authorize/token"):
            self.reply({"errorCode": 0, "result": {"accessToken": TOKEN}})
        else:
            self.reply({"errorCode": -1, "msg": f"unexpected POST {self.path}"})

    def do_GET(self):
        if self.path == "/webhook/posts":
            self.reply(WEBHOOK)
        elif self.headers.get("Authorization") != f"AccessToken={TOKEN}":
            self.reply({"errorCode": -1, "msg": "bad or missing access token"})
        elif "/clients?" in self.path:
            data = clients()
            self.reply({"errorCode": 0, "result": {"totalRows": len(data), "data": data}})
        else:
            self.reply({"errorCode": -1, "msg": f"unexpected GET {self.path}"})

    def reply(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
