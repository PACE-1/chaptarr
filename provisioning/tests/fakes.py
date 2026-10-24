"""In-process fakes for the Chaptarr API, the Audiobookshelf API and the docker CLI."""

from __future__ import annotations

import copy
import http.server
import json
import re
import subprocess
import threading
import urllib.parse
from pathlib import Path

MASK = "********"


class FakeServer:
    """Tiny JSON server: subclasses register (method, regex) -> handler(match, body, query)."""

    def __init__(self):
        self.requests: list[tuple[str, str, object]] = []
        self.fail: dict[tuple[str, str], list[tuple[int, object]]] = {}
        self.routes: list[tuple[str, re.Pattern, object]] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _go(self, method):
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw) if raw else None
                status, payload = fake.handle(method, parsed.path, body, urllib.parse.parse_qs(parsed.query),
                                              self.headers)
                data = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._go("GET")

            def do_POST(self):
                self._go("POST")

            def do_PUT(self):
                self._go("PUT")

            def do_PATCH(self):
                self._go("PATCH")

            def do_DELETE(self):
                self._go("DELETE")

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def route(self, method: str, pattern: str):
        def deco(fn):
            self.routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    def fail_next(self, method: str, path: str, status: int = 500, body: object = None, times: int = 1):
        self.fail.setdefault((method, path), []).extend([(status, body or {"message": "boom"})] * times)

    def count(self, method: str, path: str) -> int:
        return sum(1 for m, p, _ in self.requests if m == method and p == path)

    def authorized(self, headers) -> bool:
        return True

    def handle(self, method, path, body, query, headers):
        with self.lock:
            if not self.authorized(headers):
                return 401, {"message": "Unauthorized"}
            self.requests.append((method, path, body))
            pending = self.fail.get((method, path))
            if pending:
                return pending.pop(0)
            for m, rx, fn in self.routes:
                match = rx.match(path)
                if m == method and match:
                    return fn(match, body, query)
            return 404, {"message": f"no route {method} {path}"}


class FakeChaptarr(FakeServer):
    def __init__(self, api_key: str | None = None, seed_template: bool = False):
        super().__init__()
        self.api_key = api_key
        self.next_id = 100
        self.ready_after = 0  # number of status polls answered 503 first
        self.db: dict[str, list[dict]] = {k: [] for k in (
            "qualityprofile", "metadataprofile", "rootfolder", "downloadclient", "remotepathmapping",
            "indexer", "customformat")}
        self.db["customformat"] = [{"id": 7, "name": "Preferred", "builtInKey": "preferred"}]
        if seed_template:
            self._seed_template()
        else:  # a fresh instance comes with default profiles
            self.db["qualityprofile"] = [
                {"id": 1, "name": "Default Audiobook", "profileType": "audiobook", "formatItems": []},
                {"id": 2, "name": "Default E-Book", "profileType": "ebook", "formatItems": []},
            ]
            self.db["metadataprofile"] = [{"id": 1, "name": "Standard", "profileType": 0},
                                          {"id": 2, "name": "None", "profileType": 0}]
        self._routes()

    def _seed_template(self):
        self.db["customformat"] = [{"id": 31, "name": "Preferred", "builtInKey": "preferred"}]
        self.db["qualityprofile"] = [
            {"id": 1, "name": "Default Audiobook", "profileType": "audiobook", "formatItems": []},
            {"id": 5, "name": "Family E-Book", "profileType": "ebook", "cutoff": 3, "items": [{"quality": {"id": 3}}],
             "minFormatScore": 10,
             "formatItems": [{"format": 31, "name": "Preferred", "builtInKey": "preferred", "score": 50}]},
        ]
        self.db["metadataprofile"] = [{"id": 1, "name": "Standard", "profileType": 0},
                                      {"id": 4, "name": "Ebook Strict", "profileType": 2, "minPages": 20},
                                      {"id": 9, "name": "None", "profileType": 0}]
        self.db["downloadclient"] = [{
            "id": 3, "name": "qBittorrent", "implementation": "QBittorrent", "configContract": "QBittorrentSettings",
            "protocol": "torrent", "enable": True, "priority": 1, "tags": [4], "audiobookTags": [4], "ebookTags": [],
            "fields": [
                {"name": "host", "value": "qbittorrent"}, {"name": "port", "value": 8080},
                {"name": "username", "value": "admin"}, {"name": "password", "value": MASK},
                {"name": "audiobookCategory", "value": "chaptarr"}, {"name": "ebookCategory", "value": "chaptarr"},
            ]}]
        self.db["indexer"] = [{
            "id": 2, "name": "MyAnonamouse", "implementation": "MyAnonamouse", "tags": [1], "downloadClientId": 3,
            "fields": [{"name": "baseUrl", "value": "https://www.myanonamouse.net"}, {"name": "mamId", "value": MASK}],
        }]

    def authorized(self, headers) -> bool:
        return self.api_key is not None and headers.get("X-Api-Key") == self.api_key

    def _new_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def _routes(self):
        @self.route("GET", r"/api/v1/system/status")
        def status(m, b, q):
            if self.ready_after > 0:
                self.ready_after -= 1
                return 503, {"message": "starting"}
            return 200, {"version": "1.2.3"}

        @self.route("GET", r"/api/v1/qualityprofile/schema")
        def qp_schema(m, b, q):
            media = (q.get("mediaType") or [""])[0]
            return 200, {"name": f"New {media}", "profileType": media,
                         "formatItems": [{"format": c["id"], "name": c["name"], "builtInKey": c["builtInKey"],
                                          "score": 0} for c in self.db["customformat"]]}

        @self.route("GET", r"/api/v1/downloadclient/schema")
        def dc_schema(m, b, q):
            return 200, [
                {"implementation": "QBittorrent", "configContract": "QBittorrentSettings", "protocol": "torrent",
                 "enable": True, "priority": 1, "fields": [
                     {"name": "host", "value": "localhost"}, {"name": "port", "value": 8080},
                     {"name": "username", "value": ""}, {"name": "password", "value": ""},
                     {"name": "audiobookCategory", "value": "chaptarr"}, {"name": "ebookCategory", "value": "chaptarr"}]},
                {"implementation": "Sabnzbd", "configContract": "SabnzbdSettings", "protocol": "usenet",
                 "enable": True, "priority": 1, "fields": [
                     {"name": "host", "value": "localhost"}, {"name": "apiKey", "value": ""},
                     {"name": "audiobookCategory", "value": "chaptarr"}, {"name": "ebookCategory", "value": "chaptarr"}]},
            ]

        for coll in ("qualityprofile", "metadataprofile", "rootfolder", "downloadclient", "remotepathmapping",
                     "indexer", "customformat"):
            self._crud(coll)

    def _crud(self, coll: str):
        @self.route("GET", rf"/api/v1/{coll}")
        def list_(m, b, q):
            return 200, [self._public(coll, x) for x in self.db[coll]]

        @self.route("GET", rf"/api/v1/{coll}/(\d+)")
        def get_(m, b, q):
            item = next((x for x in self.db[coll] if x["id"] == int(m.group(1))), None)
            return (200, self._public(coll, item)) if item else (404, {"message": "not found"})

        @self.route("POST", rf"/api/v1/{coll}")
        def post_(m, b, q):
            err = self._validate(coll, b)
            if err:
                return 400, [{"errorMessage": err}]
            item = copy.deepcopy(b)
            item["id"] = self._new_id()
            self.db[coll].append(item)
            return 201, self._public(coll, item)

        @self.route("PUT", rf"/api/v1/{coll}/(\d+)")
        def put_(m, b, q):
            idx = next((i for i, x in enumerate(self.db[coll]) if x["id"] == int(m.group(1))), None)
            if idx is None:
                return 404, {"message": "not found"}
            item = copy.deepcopy(b)
            old = {f["name"]: f["value"] for f in self.db[coll][idx].get("fields", [])}
            for f in item.get("fields", []):  # the real API keeps the stored value for the placeholder
                if f.get("value") == MASK:
                    f["value"] = old.get(f["name"])
            self.db[coll][idx] = item
            return 202, self._public(coll, item)

    def _public(self, coll, item):
        item = copy.deepcopy(item)
        if coll in ("downloadclient", "indexer"):
            for f in item.get("fields", []):
                if f["name"] in ("password", "apiKey", "mamId") and f.get("value"):
                    f["value"] = MASK
        return item

    def _validate(self, coll, b):
        if coll == "rootfolder":
            if "folderType" not in b:
                return "folderType is required"
            media = {1: "audiobook", 2: "ebook"}.get(b["folderType"])
            if not media or not (b.get(media) or {}).get("qualityProfileId") or not (b.get(media) or {}).get("metadataProfileId"):
                return f"A {media} quality profile is required"
            if any(x["path"].rstrip("/") == b["path"].rstrip("/") for x in self.db[coll]):
                return "path already configured"
        if coll == "remotepathmapping" and not b.get("downloadClientId"):
            return "downloadClientId required"
        if coll == "qualityprofile":
            ids = sorted(i["format"] for i in b.get("formatItems", []))
            if ids != sorted(c["id"] for c in self.db["customformat"]):
                return "Every applicable Custom Format must be present"
        if coll in ("downloadclient", "indexer"):
            if any(f.get("value") == MASK for f in b.get("fields", [])):
                return "masked secret posted"
        return None


class FakeAbs(FakeServer):
    def __init__(self, api_key: str = "abs-admin-key-123456"):
        super().__init__()
        self.api_key = api_key
        self.users: dict[str, dict] = {"root1": {"id": "root1", "username": "root", "type": "root",
                                                 "permissions": {"accessAllLibraries": True}, "librariesAccessible": []}}
        self.libraries: list[dict] = []
        self.settings: dict = {}
        self.n = 0
        self._routes()

    def authorized(self, headers) -> bool:
        return headers.get("Authorization") == f"Bearer {self.api_key}"

    def _id(self, prefix):
        self.n += 1
        return f"{prefix}{self.n}"

    def _routes(self):
        @self.route("GET", r"/api/me")
        def me(m, b, q):
            return 200, self.users["root1"]

        @self.route("GET", r"/api/users")
        def users(m, b, q):
            return 200, {"users": list(self.users.values())}

        @self.route("POST", r"/api/users")
        def create_user(m, b, q):
            if any(u["username"].lower() == b["username"].lower() for u in self.users.values()):
                return 500, {"message": "Username already taken"}
            uid = self._id("usr_")
            user = {"id": uid, "username": b["username"], "type": b["type"], "permissions": b["permissions"],
                    "librariesAccessible": b.get("librariesAccessible", []), "_password": b["password"]}
            self.users[uid] = user
            return 200, {"user": {k: v for k, v in user.items() if not k.startswith("_")}}

        @self.route("GET", r"/api/users/([^/]+)")
        def get_user(m, b, q):
            u = self.users.get(m.group(1))
            return (200, {k: v for k, v in u.items() if not k.startswith("_")}) if u else (404, None)

        @self.route("PATCH", r"/api/users/([^/]+)")
        def patch_user(m, b, q):
            u = self.users.get(m.group(1))
            if not u:
                return 404, None
            u.update(b)
            return 200, {"success": True, "user": u}

        @self.route("GET", r"/api/libraries")
        def libs(m, b, q):
            return 200, {"libraries": self.libraries}

        @self.route("POST", r"/api/libraries")
        def create_lib(m, b, q):
            lib = {"id": self._id("lib_"), **b, "folders": [{"id": self._id("fol_"), **f} for f in b["folders"]]}
            self.libraries.append(lib)
            return 200, lib

        @self.route("GET", r"/api/plugins")
        def plugins(m, b, q):
            return 200, {"plugins": [self.plugin_json()], "inventory": []}

        @self.route("PATCH", r"/api/plugins/chaptarr/settings")
        def settings(m, b, q):
            # Mirrors validateSettings(manifest, values, previous, patch=true) in ABS: start from the
            # previous settings, reject unknown keys, keep a secret when the patch sends ''.
            if "userProfiles" in b and not isinstance(b.get("userProfiles"), str):
                return 400, {"error": "userProfiles must be a string"}
            schema = {f["key"]: f for f in self.plugin_schema}
            for key in b:
                if key != "enabled" and key not in schema:
                    return 400, {"error": f"Unknown settings key {key}"}
            nxt = {} if self.replace_settings else dict(self.settings)
            for key, value in b.items():
                if not (schema.get(key, {}).get("secret") and value == ""):
                    nxt[key] = value
            self.settings = nxt
            return 200, {"plugin": self.plugin_json(), "restartRequired": False}

    plugin_schema = [
        {"key": "url", "type": "text"}, {"key": "apiKey", "type": "text", "secret": True},
        {"key": "username", "type": "text"}, {"key": "password", "type": "text", "secret": True},
        {"key": "userProfiles", "type": "password", "secret": True}, {"key": "provisionerUrl", "type": "text"},
        {"key": "provisionerToken", "type": "text", "secret": True},
    ]
    replace_settings = False

    def plugin_json(self) -> dict:
        secret = {f["key"] for f in self.plugin_schema if f.get("secret")}
        return {"id": "chaptarr", "settingsSchema": self.plugin_schema,
                "settings": {k: v for k, v in self.settings.items() if k not in secret},
                "secretsSet": {k: bool(v) for k, v in self.settings.items() if k in secret}}

    def profiles(self) -> dict:
        return json.loads(self.settings.get("userProfiles") or "{}")


class FakeDocker:
    """Stands in for DockerRunner: records commands; `compose up` hands the key to the fake instance."""

    def __init__(self, instance: FakeChaptarr | None = None):
        self.instance = instance
        self.calls: list[list[str]] = []
        self.fail_on: dict[str, int] = {}
        self.containers: set[str] = set()

    def run(self, args, cwd=None, check=True, timeout=600):
        self.calls.append(list(args))
        key = " ".join(args[:1]) if args[0] != "compose" else "compose " + args[args.index("-p") + 2]
        if self.fail_on.get(key):
            self.fail_on[key] -= 1
            if check:
                from chaptarr_provision import ProvisionError
                raise ProvisionError(f"docker {key} failed (exit 1): simulated")
            return subprocess.CompletedProcess(args, 1, "", "simulated")
        if args[0] == "compose":
            project = args[args.index("-p") + 1]
            action = args[args.index("-p") + 2]
            if action == "up":
                env_file = Path(cwd, ".env")
                if env_file.exists() and self.instance is not None:
                    self.instance.api_key = env_file.read_text().strip().split("=", 1)[1]
                self.containers.add(project)
            elif action == "down":
                self.containers.discard(project)
        return subprocess.CompletedProcess(args, 0, "ok\n", "")

    def count(self, action: str) -> int:
        return sum(1 for c in self.calls if c[0] == "compose" and action in c)
