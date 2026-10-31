"""A small in-memory Distrobase registry for tests, with the API that Hydrogen uses.

GET  /v1/{author}/{distribution}                    {"latest", "releases"?}
GET  /v1/{author}/{distribution}/{release}          {"release", "artifacts": [{name, size, sha256}]}
GET  /v1/{author}/{distribution}/{release}/{name}   the artifact
POST /v1/{author}/{distribution}/{release}          multipart "artifact" upload (Basic auth)
GET  /v1/users/{username}                           (Basic auth checked when given)
POST /v1/users                                      {"username", "password"}
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Registry:
    def __init__(self, list_releases: bool = True):
        self.list_releases = list_releases
        self.users: dict[str, str] = {}
        self.releases: dict[tuple[str, str], dict[str, bytes]] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        registry = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, body, content_type="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _user(self) -> str | None:
                header = self.headers.get("Authorization", "")
                if not header.startswith("Basic "):
                    return None
                user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                return user if registry.users.get(user) == password else ""

            def do_GET(self):
                parts = [p for p in self.path.split("?")[0].split("/") if p]
                if parts[:2] == ["v1", "users"] and len(parts) == 3:
                    if parts[2] not in registry.users:
                        return self._send(404, {"detail": "User not found"})
                    if self._user() == "":
                        return self._send(401, {"detail": "Invalid credentials"})
                    return self._send(200, {"username": parts[2]})
                if len(parts) >= 3 and parts[0] == "v1":
                    releases = registry.releases.get((parts[1], parts[2]))
                    if releases is None:
                        return self._send(404, {"detail": "Distribution not found"})
                    if len(parts) == 3:
                        ordered = list(releases)
                        body = {"author": parts[1], "distribution": parts[2], "latest": ordered[-1]}
                        if registry.list_releases:
                            body["releases"] = ordered
                        return self._send(200, body)
                    data = releases.get(parts[3])
                    if data is None:
                        return self._send(404, {"detail": "Release not found"})
                    name = f"{parts[3]}.tar.gz"
                    if len(parts) == 4:
                        return self._send(200, {"release": parts[3], "author": parts[1], "distribution": parts[2],
                                                "artifacts": [{"name": name, "size": len(data),
                                                               "sha256": hashlib.sha256(data).hexdigest()}]})
                    if len(parts) == 5 and parts[4] == name:
                        return self._send(200, data, "application/gzip")
                self._send(404, {"detail": "Not Found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                parts = [p for p in self.path.split("/") if p]
                if parts == ["v1", "users"]:
                    data = json.loads(body)
                    if data["username"] in registry.users:
                        return self._send(409, {"detail": "User exists"})
                    registry.users[data["username"]] = data["password"]
                    return self._send(200, {"username": data["username"]})
                if len(parts) == 4 and parts[0] == "v1":
                    user = self._user()
                    if not user:
                        return self._send(401, {"detail": "Authentication required"})
                    if user != parts[1]:
                        return self._send(403, {"detail": "Not your distribution"})
                    message = BytesParser(policy=HTTP).parsebytes(
                        b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                    artifact = next(p for p in message.iter_parts() if p.get_param("name", header="content-disposition") == "artifact")
                    data = artifact.get_payload(decode=True)
                    releases = registry.releases.setdefault((parts[1], parts[2]), {})
                    if parts[3] in releases:
                        return self._send(409, {"detail": "Release exists"})
                    releases[parts[3]] = data
                    return self._send(200, {"name": f"{parts[3]}.tar.gz", "size": len(data),
                                            "sha256": hashlib.sha256(data).hexdigest()})
                self._send(404, {"detail": "Not Found"})

        return Handler

