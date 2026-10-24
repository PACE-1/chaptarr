"""Tests for `discover` and `install-service` (fake docker inspect, temp files, real-schema SQLite)."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import chaptarr_provision as cp  # noqa: E402
from fakes import FakeAbs, FakeDocker, FakeServer  # noqa: E402

TEMPLATE_KEY = "f" * 32
QBIT_PASS = "qbit-db-password"
SAB_KEY = "sabnzbd-db-apikey"
MAM_ID = "mam-db-cookie"
PROWLARR_KEY = "p" * 32
SHARED_KEY = "shared-admin-key-123"

# Column lists from src/NzbDrone.Core/Datastore/Migration/001_chaptarr_complete_schema.cs (+ 025 media tags).
SCHEMA = """
CREATE TABLE "DownloadClients" ("Id" INTEGER PRIMARY KEY AUTOINCREMENT, "Enable" INTEGER NOT NULL,
  "Name" TEXT NOT NULL, "Implementation" TEXT NOT NULL, "Settings" TEXT, "ConfigContract" TEXT NOT NULL,
  "Priority" INTEGER DEFAULT 1, "Tags" TEXT, "RemoveCompletedDownloads" INTEGER NOT NULL DEFAULT 1,
  "RemoveFailedDownloads" INTEGER NOT NULL DEFAULT 1, "Protocol" INTEGER, "AudiobookTags" TEXT, "EbookTags" TEXT);
CREATE TABLE "Indexers" ("Id" INTEGER PRIMARY KEY AUTOINCREMENT, "Name" TEXT NOT NULL,
  "Implementation" TEXT NOT NULL, "Settings" TEXT, "ConfigContract" TEXT NOT NULL, "Enable" INTEGER,
  "SupportsRss" INTEGER NOT NULL, "SupportsSearch" INTEGER NOT NULL, "Priority" INTEGER NOT NULL, "Tags" TEXT,
  "DownloadClientId" INTEGER DEFAULT 0, "EnableRss" INTEGER NOT NULL DEFAULT 1,
  "EnableAutomaticSearch" INTEGER NOT NULL DEFAULT 1, "EnableInteractiveSearch" INTEGER NOT NULL DEFAULT 1);
CREATE TABLE "RemotePathMappings" ("Id" INTEGER PRIMARY KEY AUTOINCREMENT, "DownloadClientId" INTEGER NOT NULL DEFAULT 0,
  "Host" TEXT NOT NULL, "RemotePath" TEXT NOT NULL, "LocalPath" TEXT NOT NULL);
CREATE TABLE "RootFolders" ("Id" INTEGER PRIMARY KEY AUTOINCREMENT, "Path" TEXT NOT NULL UNIQUE, "Name" TEXT,
  "DefaultSearchCriteriaProfileId" INTEGER, "DefaultTags" TEXT, "IsCalibreLibrary" INTEGER DEFAULT 0,
  "CalibreSettings" TEXT, "UseCalibreNaming" INTEGER DEFAULT 0, "OutputFormat" TEXT, "OutputProfile" TEXT,
  "FolderType" INTEGER NOT NULL DEFAULT 0, "AudiobookSettings" TEXT, "EbookSettings" TEXT);
"""


def container(name, image, mounts, networks, env=None, hostname=None):
    return {
        "Id": (name.encode().hex() * 8)[:64], "Name": "/" + name, "State": {"Running": True},
        "Config": {"Image": image, "Env": [f"{k}={v}" for k, v in (env or {}).items()],
                   "Hostname": hostname or name[:12], "Labels": {}},
        "Mounts": [{"Type": "bind", "Source": s, "Destination": d, "RW": rw} for s, d, rw in mounts],
        "NetworkSettings": {"Networks": {n: {"IPAddress": ip, "Aliases": aliases, "DNSNames": [name] + aliases}
                                         for n, (ip, aliases) in networks.items()}},
    }


class InspectDocker(FakeDocker):
    """FakeDocker that also answers `docker ps -q` and `docker inspect` from canned JSON."""

    def __init__(self, containers):
        super().__init__(None)
        self.inspect = containers

    def run(self, args, cwd=None, check=True, timeout=600):
        if args[0] == "ps":
            self.calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "\n".join(c["Id"] for c in self.inspect) + "\n", "")
        if args[0] == "inspect":
            self.calls.append(list(args))
            wanted = set(args[1:])
            found = [c for c in self.inspect if c["Id"] in wanted or c["Name"].lstrip("/") in wanted]
            return subprocess.CompletedProcess(args, 0 if found else 1, json.dumps(found), "")
        return super().run(args, cwd=cwd, check=check, timeout=timeout)


class Host:
    """A fake Docker host: ABS, a shared template Chaptarr, a per-user Chaptarr and Prowlarr."""

    def __init__(self, tc, abs_media_mount=True, postgres=False, prowlarr=True):
        tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(tmp.cleanup)
        self.root = r = Path(tmp.name)
        self.abs_config = r / "abs/config"
        self.tmpl_config = r / "appdata/chaptarr"
        self.media = r / "media"
        self.downloads = r / "downloads"
        for d in (self.abs_config / "plugins/.state/chaptarr", self.tmpl_config, self.media / "isaac/ebooks",
                  self.downloads, r / "appdata/prowlarr", r / "shared"):
            d.mkdir(parents=True, exist_ok=True)
        self.profiles = {
            "u_isaac": {"url": "http://chaptarr:8789", "apiKey": SHARED_KEY,
                        "ebook": {"path": "/books/isaac/ebooks", "libraryId": "l1", "label": "Isaac Ebooks"},
                        "audiobook": {"path": "/books/isaac/audiobooks", "libraryId": "l2", "label": "Isaac Audiobooks"}},
            "u_paiten": {"url": "http://chaptarr-paiten:8789", "apiKey": "b" * 32,
                         "ebook": {"path": "/books/ebooks", "libraryId": "l3", "label": "Paiten Ebooks"}},
        }
        (self.abs_config / "plugins/.state/chaptarr/settings.json").write_text(json.dumps({
            "enabled": True, "url": "http://chaptarr:8789", "apiKey": SHARED_KEY,
            "userProfiles": json.dumps(self.profiles)}))
        with contextlib.closing(sqlite3.connect(self.abs_config / "absdatabase.sqlite")) as db, db:
            db.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT)")
            db.executemany("INSERT INTO users VALUES (?, ?)", [("u_isaac", "Isaac"), ("u_paiten", "paiten")])
        (self.tmpl_config / "config.xml").write_text(f"<Config>\n  <ApiKey>{TEMPLATE_KEY}</ApiKey>\n</Config>\n")
        with contextlib.closing(sqlite3.connect(self.tmpl_config / "chaptarr.db")) as db, db:
            db.executescript(SCHEMA)
            db.execute('INSERT INTO "DownloadClients" ("Id","Enable","Name","Implementation","Settings","ConfigContract") '
                       "VALUES (1, 1, 'qBittorrent', 'QBittorrent', ?, 'QBittorrentSettings')",
                       (json.dumps({"host": "qbittorrent", "port": 8080, "username": "admin", "password": QBIT_PASS,
                                    "audiobookCategory": "chaptarr", "ebookCategory": "chaptarr"}),))
            db.execute('INSERT INTO "DownloadClients" ("Id","Enable","Name","Implementation","Settings","ConfigContract") '
                       "VALUES (2, 1, 'SABnzbd', 'Sabnzbd', ?, 'SabnzbdSettings')",
                       (json.dumps({"host": "sabnzbd", "apiKey": SAB_KEY, "ebookCategory": "books"}),))
            db.execute('INSERT INTO "Indexers" ("Name","Implementation","Settings","ConfigContract","SupportsRss",'
                       '"SupportsSearch","Priority") VALUES (\'MyAnonamouse\', \'MyAnonamouse\', ?, \'MamSettings\', 1, 1, 25)',
                       (json.dumps({"baseUrl": "https://www.myanonamouse.net", "mamId": MAM_ID}),))
            db.execute('INSERT INTO "RemotePathMappings" ("DownloadClientId","Host","RemotePath","LocalPath") '
                       "VALUES (1, 'qbittorrent', '/data/torrents/', '/downloads/')")
            db.execute('INSERT INTO "RootFolders" ("Path","FolderType") VALUES (\'/books/isaac/ebooks\', 2)')

        self.prowlarr = FakeServer()
        tc.addCleanup(self.prowlarr.close)
        self.prowlarr.route("GET", r"/api/v1/applications")(lambda m, b, q: (200, [
            {"name": "Chaptarr", "implementation": "Readarr",
             "fields": [{"name": "prowlarrUrl", "value": "http://prowlarr:9696"},
                        {"name": "baseUrl", "value": "http://chaptarr:8789"}, {"name": "apiKey", "value": "x"}]}]))
        port = self.prowlarr.httpd.server_address[1]
        (r / "appdata/prowlarr/config.xml").write_text(f"<Config><Port>{port}</Port><ApiKey>{PROWLARR_KEY}</ApiKey></Config>")

        abs_mounts = [(str(self.abs_config), "/config", True)]
        if abs_media_mount:
            abs_mounts.append((str(self.media), "/audiobooks", True))
        tmpl_env = {"PUID": "1001", "PGID": "1002", "TZ": "Europe/Berlin", "UMASK": "002"}
        if postgres:
            tmpl_env["Chaptarr__Postgres__Host"] = "postgres"
        self.containers = [
            container("audiobookshelf", "ghcr.io/advplyr/audiobookshelf:latest", abs_mounts,
                      {"media_net": ("172.20.0.2", []), "other": ("172.21.0.2", [])}),
            container("chaptarr", "ghcr.io/pace-1/chaptarr:develop",
                      [(str(self.tmpl_config), "/config", True), (str(self.media), "/books", True),
                       (str(self.downloads), "/downloads", True), (str(r / "shared"), "/shared", False)],
                      {"media_net": ("", ["chaptarr"])}, env=tmpl_env),
            container("chaptarr-paiten", "ghcr.io/pace-1/chaptarr:develop", [], {"media_net": ("172.20.0.4", [])},
                      env={"Chaptarr__Auth__ApiKey": "b" * 32}),
            container("redis", "redis:7", [], {"media_net": ("172.20.0.9", [])}),
        ]
        if prowlarr:
            self.containers.append(container("prowlarr", "lscr.io/linuxserver/prowlarr:latest",
                                             [(str(r / "appdata/prowlarr"), "/config", True)],
                                             {"media_net": ("127.0.0.1", [])}))
        self.docker = InspectDocker(self.containers)

    def discover(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cp.main(["discover", "--state-dir", str(self.root / "state"), *extra], docker=self.docker)
        return code, out.getvalue(), err.getvalue()


class DiscoverTests(unittest.TestCase):
    def test_full_discovery_writes_complete_config(self):
        h = Host(self)
        target = h.root / "etc/config.toml"
        code, out, err = h.discover("--write", str(target), "--import-profiles")
        self.assertEqual(code, 0, err)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        raw = tomllib.loads(target.read_text())

        self.assertEqual(raw["abs"]["url"], "http://audiobookshelf:80")
        self.assertEqual(raw["abs"]["config_dir"], str(h.abs_config))
        self.assertEqual(raw["docker"]["network"], "media_net")
        self.assertEqual(raw["docker"]["image"], "ghcr.io/pace-1/chaptarr:develop")
        self.assertEqual((raw["docker"]["puid"], raw["docker"]["pgid"], raw["docker"]["tz"], raw["docker"]["umask"]),
                         (1001, 1002, "Europe/Berlin", "002"))
        self.assertEqual(raw["template"], {"url": "http://chaptarr:8789", "api_key": TEMPLATE_KEY})
        self.assertEqual(raw["state_dir"], str(h.root / "state"))

        paths = raw["paths"]
        self.assertEqual(paths["config"], {"host": f"{h.root}/appdata/chaptarr-{{slug}}", "container": "/config"})
        self.assertEqual(paths["ebooks"], {"host": f"{h.media}/{{slug}}/ebooks", "container": "/books/{slug}/ebooks",
                                           "abs": "/audiobooks/{slug}/ebooks"})
        self.assertEqual(paths["audiobooks"]["host"], f"{h.media}/{{slug}}/audiobooks")
        self.assertEqual(paths["downloads"], {"host": f"{h.downloads}/chaptarr-{{slug}}",
                                              "container": "/downloads/chaptarr-{slug}"})
        self.assertEqual(sorted(raw["docker"]["extra_volumes"]),
                         sorted([f"{h.downloads}:/downloads", f"{h.root}/shared:/shared:ro"]))

        clients = {c["name"]: c for c in raw["download_clients"]}
        self.assertEqual(clients["qBittorrent"]["fields"], {"password": QBIT_PASS})
        self.assertTrue(clients["qBittorrent"]["from_template"])
        self.assertEqual(clients["qBittorrent"]["remote_path_mappings"],
                         [{"remote_path": "/data/torrents/", "local_path": "/downloads/", "host": "qbittorrent"}])
        self.assertEqual(clients["SABnzbd"]["fields"], {"apiKey": SAB_KEY})  # ebookCategory is not a secret
        self.assertEqual(raw["indexer_secrets"], {"MyAnonamouse": {"mamId": MAM_ID}})
        self.assertEqual(raw["prowlarr"]["api_key"], PROWLARR_KEY)
        self.assertEqual(raw["prowlarr"]["implementation"], "Readarr")
        self.assertEqual(raw["prowlarr"]["prowlarr_url"], "http://prowlarr:9696")

        # The generated config is accepted by the provisioner itself.
        cfg = cp.Config(raw)
        cfg.abs_api_key, cfg.secret_errors = "k" * 20, []
        cfg.validate()
        self.assertEqual(cfg.user_paths("bob", "Bob")["ebooks"]["abs"], "/audiobooks/bob/ebooks")

        profiles = json.loads((h.root / "state/profiles.json").read_text())
        self.assertEqual(profiles, h.profiles)
        for secret in (TEMPLATE_KEY, QBIT_PASS, SAB_KEY, MAM_ID, PROWLARR_KEY, SHARED_KEY, "b" * 32):
            self.assertNotIn(secret, out + err)
        self.assertIn("template: chaptarr (the plugin's shared admin connection)", out)
        self.assertIn("provisionerUrl is not set", out)
        self.assertIn("u_paiten: http://chaptarr-paiten:8789 -> container chaptarr-paiten", out)

    def test_stdout_mode_is_redacted(self):
        h = Host(self)
        code, out, err = h.discover()
        self.assertEqual(code, 0, err)
        raw = tomllib.loads(out)
        self.assertEqual(raw["template"]["api_key"], cp.REDACTED)
        self.assertEqual(raw["download_clients"][0]["fields"]["password"], cp.REDACTED)
        for secret in (TEMPLATE_KEY, QBIT_PASS, SAB_KEY, MAM_ID, PROWLARR_KEY, SHARED_KEY):
            self.assertNotIn(secret, out + err)
        self.assertIn("Discovery summary", err)
        self.assertFalse((h.root / "state/profiles.json").exists())

    def test_template_flag_and_owner_tokens(self):
        h = Host(self)
        # paiten's instance: no mounts in the fake, so path inference fails loudly rather than guessing.
        code, out, err = h.discover("--template", "paiten")
        self.assertEqual(code, 1)
        self.assertIn("no bind mount at /config", err)

    def test_abs_detection_errors(self):
        h = Host(self)
        h.docker.inspect = [c for c in h.containers if "audiobookshelf" not in c["Name"]]
        code, _, err = h.discover()
        self.assertEqual(code, 1)
        self.assertIn("no running Audiobookshelf container", err)
        self.assertIn("redis", err)
        second = json.loads(json.dumps(h.containers[0]))
        second["Name"], second["Id"] = "/abs-test", "9" * 64
        h.docker.inspect = h.containers + [second]
        code, _, err = h.discover()
        self.assertIn("several Audiobookshelf containers", err)
        self.assertIn("abs-test", err)
        code, _, err = h.discover("--abs-container", "audiobookshelf", "--write", str(h.root / "c.toml"))
        self.assertEqual(code, 0, err)

    def test_warns_when_abs_cannot_see_new_folders(self):
        h = Host(self, abs_media_mount=False)
        code, out, err = h.discover("--write", str(h.root / "c.toml"))
        self.assertEqual(code, 0, err)
        self.assertIn("outside every ABS mount", out)
        self.assertIn(f"- {h.media}:{h.media}", out)
        raw = tomllib.loads((h.root / "c.toml").read_text())
        self.assertEqual(raw["paths"]["ebooks"]["abs"], f"{h.media}/{{slug}}/ebooks")

    def test_postgres_template_falls_back_to_placeholders(self):
        h = Host(self, postgres=True, prowlarr=False)
        code, out, err = h.discover("--write", str(h.root / "c.toml"))
        self.assertEqual(code, 0, err)
        self.assertIn("PostgreSQL", out)
        self.assertIn("Prowlarr: not found", out)
        raw = tomllib.loads((h.root / "c.toml").read_text())
        self.assertNotIn("prowlarr", raw)

    def test_helpers(self):
        self.assertEqual(cp.templatize("/srv/Isaac/books", ["isaac"]), "/srv/{slug}/books")
        self.assertIsNone(cp.templatize("/srv/media/books", ["isaac"]))
        self.assertEqual(cp.sibling_template("/srv/appdata/chaptarr"), "/srv/appdata/chaptarr-{slug}")
        self.assertEqual(cp.static_prefix("/srv/media/{slug}/ebooks"), "/srv/media")
        self.assertEqual(cp.static_prefix("/srv/media/ebooks-{slug}"), "/srv/media")
        self.assertEqual(cp.static_prefix("/srv/downloads"), "/srv/downloads")
        doc = {"a": 1, "t": {"x": "y", "n": {"k": True}}, "arr": [{"name": "q", "fields": {"p": "s"},
                                                                   "maps": [{"r": "/a/"}]}], "list": ["a:b"]}
        self.assertEqual(tomllib.loads(cp.toml_dumps(doc)), doc)


class RoutingHttp(cp.HttpClient):
    """Real HTTP for the fake ABS; the provisioner container's /v1/health is answered inline."""

    def __init__(self):
        super().__init__(5)
        self.health_calls = 0

    def request(self, method, url, body=None, headers=None, timeout=None):
        if f":{cp.PROVISIONER_PORT}/v1/health" in url:
            self.health_calls += 1
            return 200, {"ok": True, "version": cp.VERSION}
        return super().request(method, url, body, headers, timeout)


class InstallServiceTests(unittest.TestCase):
    def setUp(self):
        h = self.h = Host(self)
        self.abs = FakeAbs()
        self.addCleanup(self.abs.close)
        self.abs.settings = {"enabled": True, "url": "http://chaptarr:8789", "apiKey": SHARED_KEY,
                             "userProfiles": json.dumps(h.profiles)}
        self.cfg_path = h.root / "etc/config.toml"
        code, _, err = h.discover("--write", str(self.cfg_path))
        self.assertEqual(code, 0, err)
        raw = tomllib.loads(self.cfg_path.read_text())
        raw["abs"]["url"] = self.abs.url
        raw["stacks_dir"] = str(h.root / "stacks")
        cp.atomic_write(self.cfg_path, cp.toml_dumps(raw))
        self.key_file = h.root / "abs-key"
        self.key_file.write_text(self.abs.api_key + "\n")
        h.docker.inspect.append(container("chaptarr-provisioner", "x", [], {"media_net": ("172.20.0.50", [])}))
        self.http = RoutingHttp()

    def install(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cp.main(["--config", str(self.cfg_path), "install-service", "--abs-api-key-file",
                            str(self.key_file), *extra], docker=self.h.docker, http=self.http)
        return code, out.getvalue(), err.getvalue()

    def test_install_is_safe_and_idempotent(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, out + err)
        token_file = self.cfg_path.parent / "token"
        token = token_file.read_text().strip()
        self.assertEqual(stat.S_IMODE(token_file.stat().st_mode), 0o600)
        key_copy = self.cfg_path.parent / "abs_api_key"
        self.assertEqual(key_copy.read_text().strip(), self.abs.api_key)
        self.assertEqual(stat.S_IMODE(key_copy.stat().st_mode), 0o600)

        compose = (self.h.root / "stacks/chaptarr-provisioner/compose.yaml").read_text()
        self.assertNotRegex(compose, r"(?m)^\s*ports:")
        self.assertIn('"/var/run/docker.sock:/var/run/docker.sock"', compose)
        self.assertIn(f'"{self.h.media}:{self.h.media}"', compose)
        self.assertIn(f'"{self.h.downloads}:{self.h.downloads}"', compose)
        self.assertIn(f'"{self.h.root}/appdata:{self.h.root}/appdata"', compose)
        self.assertIn(f'"{self.cfg_path.parent}:{self.cfg_path.parent}"', compose)
        self.assertIn('name: "media_net"', compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertGreaterEqual(self.http.health_calls, 1)

        s = self.abs.settings
        self.assertEqual(s["provisionerUrl"], "http://chaptarr-provisioner:8790")
        self.assertEqual(s["provisionerToken"], token)
        self.assertEqual(s["apiKey"], SHARED_KEY)  # partial PATCH kept the secrets
        self.assertEqual(json.loads(s["userProfiles"]), self.h.profiles)
        self.assertNotIn(token, out + err)
        self.assertNotIn(self.abs.api_key, out + err)

        code, out2, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(token_file.read_text().strip(), token)
        self.assertIn("unchanged", out2)
        self.assertEqual(self.abs.settings["provisionerToken"], token)

    def test_reaches_abs_by_container_ip_from_the_host(self):
        raw = tomllib.loads(self.cfg_path.read_text())
        port = self.abs.httpd.server_address[1]
        raw["abs"]["url"] = f"http://audiobookshelf.invalid:{port}"
        cp.atomic_write(self.cfg_path, cp.toml_dumps(raw))
        abs_c = next(c for c in self.h.docker.inspect if c["Name"] == "/audiobookshelf")
        abs_c["NetworkSettings"]["Networks"]["media_net"]["IPAddress"] = "127.0.0.1"
        code, out, err = self.install()
        self.assertEqual(code, 0, out + err)
        self.assertIn("provisionerToken", self.abs.settings)

    def test_refuses_patch_when_plugin_lacks_keys(self):
        self.abs.plugin_schema = [f for f in FakeAbs.plugin_schema if f["key"] != "provisionerToken"]
        code, out, _ = self.install()
        self.assertEqual(code, 1)
        self.assertIn("ABS plugin NOT updated", out)
        self.assertIn("Provisioner address: http://chaptarr-provisioner:8790", out)
        self.assertEqual(self.abs.count("PATCH", "/api/plugins/chaptarr/settings"), 0)
        self.assertNotIn("provisionerUrl", self.abs.settings)

    def test_detects_abs_that_replaces_settings(self):
        self.abs.replace_settings = True
        code, out, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("dropped secret setting(s) apiKey, userProfiles", err)

    def test_plan_settings_patch(self):
        desired = {"provisionerUrl": "u", "provisionerToken": "t"}
        self.assertFalse(cp.plan_settings_patch(None, desired)[0])
        self.assertFalse(cp.plan_settings_patch({"settingsSchema": [{"key": "provisionerUrl"}], "secretsSet": {}},
                                                desired)[0])
        self.assertFalse(cp.plan_settings_patch({"settingsSchema": FakeAbs.plugin_schema}, desired)[0])
        self.assertTrue(cp.plan_settings_patch({"settingsSchema": FakeAbs.plugin_schema, "secretsSet": {}},
                                               desired)[0])


if __name__ == "__main__":
    unittest.main()
