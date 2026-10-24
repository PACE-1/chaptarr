"""Tests for chaptarr_provision. Run: python3 -m unittest discover provisioning/tests"""

from __future__ import annotations

import io
import json
import logging
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import chaptarr_provision as cp  # noqa: E402
from fakes import MASK, FakeAbs, FakeChaptarr, FakeDocker  # noqa: E402

TOKEN = "test-token-0123456789abcdef"
QBIT_PASSWORD = "qbit-secret-pass"
MAM_ID = "mam-secret-cookie-value"
EXISTING = {
    "u_isaac": {"url": "http://chaptarr:8789", "apiKey": "a" * 32,
                "ebook": {"path": "/books/isaac/ebooks", "libraryId": "lib_i1", "label": "Isaac Ebooks"},
                "audiobook": {"path": "/books/isaac/audiobooks", "libraryId": "lib_i2", "label": "Isaac Audiobooks"}},
    "u_paiten": {"url": "http://chaptarr-paiten:8789", "apiKey": "b" * 32,
                 "ebook": {"path": "/books/ebooks", "libraryId": "lib_p1", "label": "Paiten Ebooks"}},
    "u_kristina": {"url": "http://chaptarr-kristina:8789", "apiKey": "c" * 32,
                   "audiobook": {"path": "/books/audiobooks", "libraryId": "lib_k2", "label": "Kristina Audiobooks"}},
}


class Env:
    """A temp dir, fake servers and a Provisioner wired together."""

    def __init__(self, tc: unittest.TestCase, template: bool = True, extra: dict | None = None):
        self.tmp = tempfile.TemporaryDirectory()
        tc.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.abs = FakeAbs()
        self.inst = FakeChaptarr()
        self.tmpl = FakeChaptarr(api_key="template-key-abcdef", seed_template=True) if template else None
        for s in (self.abs, self.inst, self.tmpl):
            if s:
                tc.addCleanup(s.close)
        self.docker = FakeDocker(self.inst)
        raw = {
            "state_dir": str(self.root / "state"),
            "stacks_dir": str(self.root / "stacks"),
            "hooks_dir": str(self.root / "hooks.d"),
            "server": {"token": TOKEN},
            "abs": {"url": self.abs.url, "api_key": self.abs.api_key},
            "docker": {"network": "abs_net", "provisioner_url": self.inst.url, "ready_interval": 0,
                       "ready_timeout": 5, "extra_volumes": [str(self.root / "dl") + ":/downloads"]},
            "paths": {
                "config": {"host": str(self.root / "data/{slug}/config"), "container": "/config"},
                "ebooks": {"host": str(self.root / "media/{slug}/ebooks"), "container": "/books/ebooks",
                           "abs": "/media/{slug}/ebooks"},
                "audiobooks": {"host": str(self.root / "media/{slug}/audiobooks"), "container": "/books/audiobooks",
                               "abs": "/media/{slug}/audiobooks"},
                "downloads": {"host": str(self.root / "dl/chaptarr-{slug}"), "container": "/downloads/chaptarr-{slug}"},
            },
            "download_clients": [{
                "name": "qBittorrent", "implementation": "QBittorrent",
                "fields": {"password": QBIT_PASSWORD},
                "remote_path_mappings": [{"remote_path": "/data/torrents/", "local_path": "/downloads/"}],
            }],
            "indexer_secrets": {"MyAnonamouse": {"mamId": MAM_ID}},
        }
        if self.tmpl:
            raw["template"] = {"url": self.tmpl.url, "api_key": self.tmpl.api_key}
        for k, v in (extra or {}).items():
            if isinstance(v, dict) and isinstance(raw.get(k), dict):
                raw[k].update(v)
            else:
                raw[k] = v
        self.raw = raw
        self.cfg = cp.Config(raw)
        self.cfg.validate()
        self.p = cp.Provisioner(self.cfg, docker=self.docker, sleep=lambda s: None)

    def state(self, slug: str) -> dict:
        return json.loads((self.root / "state/users" / f"{slug}.json").read_text())


class CreateTests(unittest.TestCase):
    def test_full_create(self):
        env = Env(self)
        events = []
        res = env.p.create("Alice", password="pw-alice-12345", progress=lambda *a: events.append(a))

        self.assertEqual(res["slug"], "alice")
        self.assertEqual(res["url"], "http://chaptarr-alice:8789")
        self.assertNotIn("apiKey", json.dumps(res))
        self.assertEqual([e[0] for e in events if e[1] == "done"], cp.STEPS)

        # ABS user: type user, no access to all libraries, download allowed, then only its two libraries
        user = env.abs.users[res["absUserId"]]
        self.assertEqual(user["type"], "user")
        self.assertFalse(user["permissions"]["accessAllLibraries"])
        self.assertTrue(user["permissions"]["download"])
        self.assertEqual(sorted(user["librariesAccessible"]), sorted([res["ebook"]["libraryId"], res["audiobook"]["libraryId"]]))
        libs = {lib["name"]: lib for lib in env.abs.libraries}
        self.assertEqual(libs["Alice Ebooks"]["folders"][0]["fullPath"], "/media/alice/ebooks")
        self.assertEqual(libs["Alice Audiobooks"]["mediaType"], "book")

        # Folders, compose, .env
        for sub in ("data/alice/config", "media/alice/ebooks", "media/alice/audiobooks",
                    "dl/chaptarr-alice/ebook", "dl/chaptarr-alice/audiobook"):
            self.assertTrue((env.root / sub).is_dir(), sub)
        stack = env.root / "stacks/chaptarr-alice"
        compose = (stack / "compose.yaml").read_text()
        state = env.state("alice")
        self.assertIn('container_name: "chaptarr-alice"', compose)
        self.assertIn('name: "abs_net"', compose)
        self.assertIn("external: true", compose)
        self.assertIn("Chaptarr__Auth__ApiKey: ${CHAPTARR_API_KEY", compose)
        self.assertIn('"ghcr.io/pace-1/chaptarr:develop"', compose)
        self.assertNotRegex(compose, r"(?m)^\s*ports:")
        self.assertNotIn(state["apiKey"], compose)
        self.assertEqual((stack / ".env").read_text().strip(), f"CHAPTARR_API_KEY={state['apiKey']}")
        self.assertEqual(stat.S_IMODE((stack / ".env").stat().st_mode), 0o600)
        self.assertRegex(state["apiKey"], r"^[0-9a-f]{32}$")

        # State file: 0600, every step done, no password
        sp = env.root / "state/users/alice.json"
        self.assertEqual(stat.S_IMODE(sp.stat().st_mode), 0o600)
        self.assertEqual(state["state"], "provisioned")
        self.assertTrue(all(state["steps"][s]["state"] == "done" for s in cp.STEPS))
        self.assertNotIn("pw-alice-12345", sp.read_text())

        # Chaptarr config
        db = env.inst.db
        qp = next(p for p in db["qualityprofile"] if p["name"] == "Family E-Book")
        self.assertEqual(qp["formatItems"], [{"format": 7, "name": "Preferred", "builtInKey": "preferred", "score": 50}])
        self.assertTrue(any(p["name"] == "Ebook Strict" for p in db["metadataprofile"]))
        self.assertFalse(sum(p["name"] == "None" for p in db["metadataprofile"]) > 1)
        rfs = {r["folderType"]: r for r in db["rootfolder"]}
        self.assertEqual(rfs[2]["path"], "/books/ebooks")
        self.assertEqual(rfs[1]["path"], "/books/audiobooks")
        self.assertEqual(rfs[2]["ebook"]["qualityProfileId"], 2)  # first ebook profile on the instance
        strict = next(p["id"] for p in db["metadataprofile"] if p["name"] == "Ebook Strict")
        self.assertEqual(rfs[2]["ebook"]["metadataProfileId"], strict)  # ebook-typed profile beats "general"
        self.assertEqual(rfs[1]["audiobook"]["metadataProfileId"], 1)  # no audiobook-typed one: "Standard"
        dc = db["downloadclient"][0]
        fields = cp.fields_to_dict(dc)
        self.assertEqual(fields["ebookCategory"], "chaptarr-alice-ebook")
        self.assertEqual(fields["audiobookCategory"], "chaptarr-alice-audiobook")
        self.assertEqual(fields["password"], QBIT_PASSWORD)
        self.assertEqual(dc["tags"], [])
        rpm = db["remotepathmapping"][0]
        self.assertEqual((rpm["downloadClientId"], rpm["host"], rpm["remotePath"], rpm["localPath"]),
                         (dc["id"], "qbittorrent", "/data/torrents/", "/downloads/"))
        idx = db["indexer"][0]
        self.assertEqual(cp.fields_to_dict(idx)["mamId"], MAM_ID)
        self.assertEqual(idx["downloadClientId"], 0)

        # Registered profile, exact shape
        profiles = env.abs.profiles()
        self.assertEqual(profiles, {res["absUserId"]: {
            "url": "http://chaptarr-alice:8789", "apiKey": state["apiKey"],
            "ebook": {"path": "/books/ebooks", "libraryId": res["ebook"]["libraryId"], "label": "Alice Ebooks"},
            "audiobook": {"path": "/books/audiobooks", "libraryId": res["audiobook"]["libraryId"],
                          "label": "Alice Audiobooks"}}})
        self.assertEqual(json.loads((env.root / "state/profiles.json").read_text()), profiles)

    def test_existing_abs_user_and_wait_ready_polls(self):
        env = Env(self, template=False)
        env.abs.users["u9"] = {"id": "u9", "username": "Bob", "type": "user",
                               "permissions": {"accessAllLibraries": False}, "librariesAccessible": ["shared"]}
        env.inst.ready_after = 2
        res = env.p.create("Bob", abs_user_id="u9")
        self.assertEqual(res["absUserId"], "u9")
        self.assertEqual(env.abs.count("POST", "/api/users"), 0)
        self.assertIn("shared", env.abs.users["u9"]["librariesAccessible"])
        self.assertEqual(len(env.abs.users["u9"]["librariesAccessible"]), 3)
        self.assertGreaterEqual(env.inst.count("GET", "/api/v1/system/status"), 3)

    def test_resume_after_failed_step(self):
        env = Env(self)
        env.inst.fail_next("POST", "/api/v1/rootfolder", 500, {"message": "disk on fire"})
        with self.assertRaises(cp.ProvisionError) as ctx:
            env.p.create("carol", password="pw-carol-12345")
        self.assertIn("chaptarr_config", str(ctx.exception))
        st = env.state("carol")
        self.assertEqual(st["state"], "failed")
        self.assertEqual(st["steps"]["chaptarr_config"]["state"], "failed")
        self.assertIn("disk on fire", st["steps"]["chaptarr_config"]["error"])
        self.assertEqual(st["steps"]["abs_user"]["state"], "done")
        self.assertNotIn("abs_libraries", st["steps"])
        self.assertEqual(env.p.list_users()[0]["state"], "failed")

        # Resume: no password needed now, ABS user not re-created, same key, no duplicates.
        key = st["apiKey"]
        res = env.p.create("carol")
        self.assertEqual(env.abs.count("POST", "/api/users"), 1)
        st = env.state("carol")
        self.assertEqual(st["state"], "provisioned")
        self.assertEqual(st["apiKey"], key)
        self.assertEqual(len(env.inst.db["rootfolder"]), 2)
        self.assertEqual(sum(p["name"] == "Family E-Book" for p in env.inst.db["qualityprofile"]), 1)
        self.assertIn(res["absUserId"], env.abs.profiles())

    def test_resume_after_abs_user_created_but_not_recorded(self):
        env = Env(self)
        # The ABS user gets created, but the run dies before the new id is recorded.
        orig = env.p._step_abs_user

        def crash(state, ctx):
            orig(state, ctx)
            del state["absUserId"]
            raise RuntimeError("power cut")
        env.p._step_abs_user = crash
        with self.assertRaises(cp.ProvisionError):
            env.p.create("dave", password="pw-dave-123456")
        env.p._step_abs_user = orig
        res = env.p.create("dave")  # adopts the user the earlier run created
        self.assertEqual(env.abs.count("POST", "/api/users"), 1)
        self.assertEqual(env.abs.users[res["absUserId"]]["username"], "dave")

    def test_refuses_to_adopt_foreign_abs_user(self):
        env = Env(self)
        env.abs.users["x1"] = {"id": "x1", "username": "erin", "type": "user", "permissions": {}}
        with self.assertRaisesRegex(cp.ProvisionError, "already exists"):
            env.p.create("erin", password="pw-erin-123456")

    def test_password_required_to_create_abs_user(self):
        env = Env(self)
        with self.assertRaisesRegex(cp.ProvisionError, "password is required"):
            env.p.create("frank")

    def test_idempotent_rerun(self):
        env = Env(self)
        first = env.p.create("gina", password="pw-gina-123456")
        snapshot = {k: len(v) for k, v in env.inst.db.items()}
        libs = len(env.abs.libraries)
        second = env.p.create("gina")
        self.assertEqual(first, second)
        self.assertEqual({k: len(v) for k, v in env.inst.db.items()}, snapshot)
        self.assertEqual(len(env.abs.libraries), libs)
        self.assertEqual(env.abs.count("POST", "/api/users"), 1)
        self.assertEqual(env.docker.count("up"), 2)  # container step is re-applied (self-healing)
        self.assertEqual(len(env.abs.profiles()), 1)

    def test_lock_prevents_concurrent_create(self):
        env = Env(self)
        with env.p.store.lock("hank"):
            with self.assertRaises(cp.BusyError):
                env.p.create("hank", password="pw-hank-123456")

    def test_missing_secret_in_template_client_fails_clearly(self):
        env = Env(self, extra={"download_clients": [{"name": "qBittorrent"}]})
        with self.assertRaisesRegex(cp.ProvisionError, "password come back masked"):
            env.p.create("ivy", password="pw-ivy-1234567")

    def test_hooks_get_result_without_secrets(self):
        env = Env(self)
        hooks = env.root / "hooks.d"
        hooks.mkdir()
        out = env.root / "hook-out.json"
        hook = hooks / "10-record"
        hook.write_text(f"#!/bin/sh\ncat > {out}\n")
        hook.chmod(0o755)
        (hooks / "20-not-executable").write_text("#!/bin/sh\nexit 1\n")
        env.p.create("jack", password="pw-jack-123456")
        payload = json.loads(out.read_text())
        self.assertEqual(payload["url"], "http://chaptarr-jack:8789")
        self.assertNotIn(env.state("jack")["apiKey"], out.read_text())
        self.assertNotIn("pw-jack", out.read_text())
        self.assertIn("10-record ok", env.state("jack")["steps"]["hooks"]["detail"])


class SlugTests(unittest.TestCase):
    def test_slugify(self):
        self.assertEqual(cp.slugify("Isaac"), "isaac")
        self.assertEqual(cp.slugify("  Mary Jane_O'Neil "), "mary-jane-o-neil")
        self.assertEqual(cp.slugify("a--b"), "a-b")
        self.assertEqual(len(cp.slugify("x" * 80)), 32)
        for bad in ("", "   ", "!!!", "__"):
            with self.assertRaises(cp.ValidationError):
                cp.slugify(bad)
        for bad in ("Upper", "-lead", "trail-", "a/b", "../x", "x" * 33):
            with self.assertRaises(cp.ValidationError):
                cp.validate_slug(bad)

    def test_slug_collision(self):
        env = Env(self)
        env.p.create("Kim", password="pw-kim-1234567")
        with self.assertRaisesRegex(cp.ValidationError, "already used"):
            env.p.create("kim!", password="pw-kim-1234567")
        # The same user (any case) is a resume, not a collision.
        env.p.create("KIM")

    def test_abs_user_already_has_other_instance(self):
        env = Env(self)
        res = env.p.create("lee", password="pw-lee-1234567")
        with self.assertRaisesRegex(cp.ValidationError, "already has instance"):
            env.p.create("lee2", abs_user_id=res["absUserId"])

    def test_bad_username_in_path(self):
        with self.assertRaises(cp.ValidationError):
            cp.render_path("/srv/{username}/x", "ab", "../etc")


class ProfileTests(unittest.TestCase):
    def test_import_then_create_keeps_existing_users(self):
        env = Env(self)
        # The plugin stores userProfiles as a JSON string inside its settings.
        out = env.p.import_profiles(json.dumps({"userProfiles": json.dumps(EXISTING)}))
        self.assertEqual(out["added"], sorted(EXISTING))
        rows = {r["absUserId"]: r for r in env.p.list_users()}
        self.assertEqual(rows["u_paiten"]["state"], "imported")
        self.assertEqual(rows["u_paiten"]["slug"], "paiten")
        res = env.p.create("mona", password="pw-mona-123456")
        profiles = env.abs.profiles()
        self.assertEqual(set(profiles), set(EXISTING) | {res["absUserId"]})
        for uid, prof in EXISTING.items():
            self.assertEqual(profiles[uid], prof)

    def test_import_accepts_plain_and_string_forms(self):
        for text in (json.dumps(EXISTING), json.dumps(json.dumps(EXISTING))):
            self.assertEqual(cp.parse_profiles_input(text), EXISTING)

    def test_rejects_duplicate_url_or_key(self):
        dup_url = dict(EXISTING, u_new={**EXISTING["u_paiten"], "apiKey": "d" * 32})
        with self.assertRaisesRegex(cp.ValidationError, "same url"):
            cp.validate_profiles(dup_url)
        dup_key = dict(EXISTING, u_new={**EXISTING["u_paiten"], "url": "http://chaptarr-new:8789"})
        with self.assertRaisesRegex(cp.ValidationError, "same apiKey"):
            cp.validate_profiles(dup_key)
        env = Env(self)
        env.p.import_profiles(json.dumps(EXISTING))
        with self.assertRaises(cp.ValidationError):
            env.p.import_profiles(json.dumps({"u_new": dup_key["u_new"]}))
        self.assertEqual(set(env.p.store.load_profiles()), set(EXISTING))

    def test_rejects_bad_paths(self):
        bad = {"u": {"url": "http://x:1", "apiKey": "k", "ebook": {"path": "/books/../etc"}}}
        with self.assertRaisesRegex(cp.ValidationError, r"\.\."):
            cp.validate_profiles(bad)
        bad["u"]["ebook"]["path"] = "relative/path"
        with self.assertRaisesRegex(cp.ValidationError, "absolute"):
            cp.validate_profiles(bad)

    def test_create_refuses_user_with_imported_profile(self):
        env = Env(self)
        env.p.import_profiles(json.dumps(EXISTING))
        with self.assertRaisesRegex(cp.ValidationError, "did not create"):
            env.p.create("isaac", abs_user_id="u_isaac")
        # A new user whose url would collide with an imported profile is refused too.
        env.abs.users["u_other"] = {"id": "u_other", "username": "paiten", "type": "user", "permissions": {}}
        with self.assertRaisesRegex(cp.ValidationError, "already used by the profile"):
            env.p.create("paiten", abs_user_id="u_other")


class MaskingTests(unittest.TestCase):
    def test_masker(self):
        m = cp.Masker()
        m.add("supersecretvalue", "abc", None, MASK)
        text = m.mask('x supersecretvalue y abc {"apiKey": "zzz9", "password":"p4ss"} apikey=qq Bearer tok.en')
        self.assertNotIn("supersecretvalue", text)
        self.assertIn("abc", text)  # too short to be registered
        self.assertNotIn("zzz9", text)
        self.assertNotIn("p4ss", text)
        self.assertNotIn("qq", text)
        self.assertNotIn("tok.en", text)

    def test_errors_in_state_api_and_logs_are_masked(self):
        env = Env(self)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(cp.MaskingFilter(env.p.masker))
        cp.LOG.addHandler(handler)
        self.addCleanup(cp.LOG.removeHandler, handler)
        # The instance echoes secrets back in its error (the key it was sent, the qBittorrent password).
        holder = {}

        def echo(m, b, q):
            return 400, {"message": f"bad key {env.inst.api_key} and password {QBIT_PASSWORD}"}
        env.inst.routes.insert(0, ("POST", __import__("re").compile("^/api/v1/downloadclient$"), echo))
        with self.assertRaises(cp.ProvisionError) as ctx:
            env.p.create("nina", password="pw-nina-secret-1")
        holder["key"] = env.state("nina")["apiKey"]
        state_text = (env.root / "state/users/nina.json").read_text()
        for blob in (str(ctx.exception), json.dumps(env.p.status("nina")), json.dumps(env.p.list_users()),
                     stream.getvalue()):
            self.assertNotIn(holder["key"], blob)
            self.assertNotIn(QBIT_PASSWORD, blob)
            self.assertNotIn("pw-nina-secret-1", blob)
        self.assertNotIn(QBIT_PASSWORD, state_text)
        self.assertIn("***", env.state("nina")["steps"]["chaptarr_config"]["error"])
        self.assertNotIn("apiKey", env.p.status("nina"))


class ShelfmarkTests(unittest.TestCase):
    def _add_shelfmark_client(self, inst):
        inst.db["downloadclient"].append({"id": 900, "name": "Shelfmark", "implementation": "Shelfmark",
                                          "fields": [{"name": "url", "value": "http://shelfmark:8084"},
                                                     {"name": "apiKey", "value": "sm-secret"},
                                                     {"name": "shelfmarkUserId", "value": ""}]})

    def test_applied_when_client_and_mapping_exist(self):
        env = Env(self, extra={"shelfmark": {"users": {"olga": "sm-42"}}})
        self._add_shelfmark_client(env.inst)
        env.p.create("olga", password="pw-olga-123456")
        client = next(c for c in env.inst.db["downloadclient"] if c["id"] == 900)
        self.assertEqual(cp.fields_to_dict(client)["shelfmarkUserId"], "sm-42")
        self.assertEqual(cp.fields_to_dict(client)["apiKey"], "sm-secret")  # placeholder kept the secret
        self.assertIn("set on 1", env.state("olga")["steps"]["shelfmark"]["detail"])
        self.assertEqual(env.inst.count("PUT", "/api/v1/downloadclient/900"), 1)

    def test_skipped_without_mapping_or_client(self):
        env = Env(self)
        self._add_shelfmark_client(env.inst)
        env.p.create("pete", password="pw-pete-123456")
        self.assertIn("no Shelfmark user mapped", env.state("pete")["steps"]["shelfmark"]["detail"])
        env2 = Env(self, extra={"shelfmark": {"users": {"quinn": "sm-1"}}})
        env2.p.create("quinn", password="pw-quinn-12345")
        self.assertIn("no Shelfmark download client", env2.state("quinn")["steps"]["shelfmark"]["detail"])
        self.assertEqual(env.inst.count("PUT", "/api/v1/downloadclient/900"), 0)


class DeleteTests(unittest.TestCase):
    def test_delete_without_purge_keeps_data(self):
        env = Env(self)
        env.p.import_profiles(json.dumps(EXISTING))
        res = env.p.create("rita", password="pw-rita-123456")
        marker = env.root / "media/rita/ebooks/book.epub"
        marker.write_text("x")
        out = env.p.delete("rita")
        self.assertTrue(out["deregistered"])
        self.assertEqual(env.docker.count("down"), 1)
        self.assertTrue(marker.exists())
        self.assertTrue((env.root / "data/rita/config").is_dir())
        self.assertNotIn(res["absUserId"], env.abs.profiles())
        self.assertEqual(set(env.abs.profiles()), set(EXISTING))
        self.assertEqual(env.state("rita")["state"], "deleted")
        # Re-create brings it back with the same key and data.
        key = env.state("rita")["apiKey"]
        env.p.create("rita")
        self.assertEqual(env.abs.profiles()[res["absUserId"]]["apiKey"], key)

    def test_delete_with_purge_removes_data_and_state(self):
        env = Env(self)
        env.p.create("sam", password="pw-sam-1234567")
        out = env.p.delete("sam", purge=True)
        self.assertTrue(out["purged"])
        self.assertFalse((env.root / "media/sam").exists() and any((env.root / "media/sam").iterdir()))
        self.assertFalse((env.root / "data/sam/config").exists())
        self.assertFalse((env.root / "stacks/chaptarr-sam").exists())
        self.assertFalse((env.root / "state/users/sam.json").exists())
        self.assertEqual(env.abs.profiles(), {})

    def test_delete_unknown(self):
        env = Env(self)
        with self.assertRaises(cp.ValidationError):
            env.p.delete("nobody")


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(self)
        self.server = cp.make_server(self.env.p, "127.0.0.1:0", TOKEN)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, method, path, body=None, token=TOKEN):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def wait_job(self, job_id):
        for _ in range(200):
            status, job = self.call("GET", f"/v1/jobs/{job_id}")
            if job["state"] in ("done", "failed"):
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_auth(self):
        self.assertEqual(self.call("GET", "/v1/health", token=None)[0], 401)
        self.assertEqual(self.call("GET", "/v1/health", token="wrong-token-wrong-token")[0], 401)
        self.assertEqual(self.call("GET", "/v1/users", token="")[0], 401)
        self.assertEqual(self.call("POST", "/v1/users", {"username": "x", "password": "y"}, token="nope")[0], 401)
        status, body = self.call("GET", "/v1/health")
        self.assertEqual((status, body), (200, {"ok": True, "version": cp.VERSION}))

    def test_refuses_to_start_without_token(self):
        for token in (None, "", "short"):
            with self.assertRaises(cp.ConfigError):
                cp.make_server(self.env.p, "127.0.0.1:0", token)

    def test_create_job_list_and_delete(self):
        self.assertEqual(self.call("POST", "/v1/users", {"username": "tess"})[0], 400)  # no password
        self.assertEqual(self.call("POST", "/v1/users", {"username": "!!!", "password": "x"})[0], 400)
        status, body = self.call("POST", "/v1/users", {"username": "tess", "password": "pw-tess-123456"})
        self.assertEqual(status, 202)
        job = self.wait_job(body["jobId"])
        self.assertEqual(job["state"], "done", job)
        self.assertEqual([s["name"] for s in job["steps"]], cp.STEPS)
        self.assertTrue(all(s["state"] == "done" for s in job["steps"]))
        self.assertEqual(job["result"]["url"], "http://chaptarr-tess:8789")
        key = self.env.state("tess")["apiKey"]
        status, users = self.call("GET", "/v1/users")
        self.assertEqual(status, 200)
        self.assertEqual(users[0]["state"], "provisioned")
        self.assertEqual(users[0]["lastJobId"], body["jobId"])
        self.assertEqual(set(users[0]) >= {"absUserId", "username", "slug", "state", "url", "createdAt"}, True)
        for path in ("/v1/users", f"/v1/jobs/{body['jobId']}", "/v1/users/tess"):
            self.assertNotIn(key, json.dumps(self.call("GET", path)[1]))
        # Re-POST resumes (new job, same result, nothing duplicated).
        status, body2 = self.call("POST", "/v1/users", {"username": "tess"})
        self.assertEqual(self.wait_job(body2["jobId"])["state"], "done")
        self.assertEqual(self.env.abs.count("POST", "/api/users"), 1)
        status, out = self.call("DELETE", "/v1/users/tess?purge=false")
        self.assertEqual((status, out["purged"], out["deregistered"]), (200, False, True))
        self.assertTrue((self.env.root / "data/tess/config").is_dir())
        self.assertEqual(self.call("GET", "/v1/jobs/nope")[0], 404)
        self.assertEqual(self.call("GET", "/v1/nothing")[0], 404)
        self.assertEqual(self.call("DELETE", "/v1/users/BAD!")[0], 400)

    def test_failed_job_reports_masked_error(self):
        self.env.inst.fail_next("POST", "/api/v1/rootfolder", 500, {"message": "nope"})
        status, body = self.call("POST", "/v1/users", {"username": "uma", "password": "pw-uma-secret-1"})
        job = self.wait_job(body["jobId"])
        self.assertEqual(job["state"], "failed")
        failed = [s for s in job["steps"] if s["state"] == "failed"]
        self.assertEqual(failed[0]["name"], "chaptarr_config")
        self.assertNotIn("pw-uma-secret-1", json.dumps(job))
        status, users = self.call("GET", "/v1/users")
        self.assertEqual(users[0]["state"], "failed")
        self.assertIn("error", users[0])
        # Retry resumes from the failed step.
        status, body = self.call("POST", "/v1/users", {"username": "uma"})
        job = self.wait_job(body["jobId"])
        self.assertEqual(job["state"], "done")


class CliTests(unittest.TestCase):
    def test_cli_create_list_status_import(self):
        env = Env(self)
        cfg_path = env.root / "config.toml"
        cfg_path.write_text(_to_toml(env.raw))
        import chaptarr_provision as mod
        orig = mod.Provisioner.__init__

        def init(self, config, http=None, docker=None, sleep=None, masker=None, clock=time.monotonic):
            orig(self, config, http=http, docker=env.docker, sleep=lambda s: None, masker=masker, clock=clock)
        mod.Provisioner.__init__ = init
        self.addCleanup(setattr, mod.Provisioner, "__init__", orig)

        def run(*argv, stdin=""):
            out, err = io.StringIO(), io.StringIO()
            old_stdin = sys.stdin
            sys.stdin = io.StringIO(stdin)
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    code = mod.main(["--config", str(cfg_path), *argv])
            finally:
                sys.stdin = old_stdin
            return code, out.getvalue(), err.getvalue()

        code, out, err = run("import-profiles", "-", stdin=json.dumps(EXISTING))
        self.assertEqual(code, 0, err)
        code, out, err = run("create", "vera", "--password-stdin", stdin="pw-vera-123456\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["slug"], "vera")
        self.assertNotIn("pw-vera-123456", out + err)
        code, out, _ = run("list", "--json")
        self.assertEqual({r["slug"] for r in json.loads(out)}, {"vera", "paiten", "kristina", None})
        code, out, _ = run("status", "vera")
        self.assertEqual(json.loads(out)["state"], "provisioned")
        self.assertNotIn(env.state("vera")["apiKey"], out)
        code, out, err = run("create", "!!!", "--password-stdin", stdin="x\n")
        self.assertEqual(code, 2)
        self.assertIn("slug", err)
        code, out, _ = run("delete", "vera")
        self.assertEqual(code, 0)


def _to_toml(raw: dict) -> str:
    """Minimal TOML writer for the test config (strings, numbers, lists, nested tables, array of tables)."""
    lines: list[str] = []

    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{" + ", ".join(f"{json.dumps(k)} = {val(x)}" for k, x in v.items()) + "}"
        return json.dumps(v)

    def table(prefix, d):
        def nested(v):
            return isinstance(v, dict) or (isinstance(v, list) and v and isinstance(v[0], dict))
        scalars = {k: v for k, v in d.items() if not nested(v)}
        if prefix:
            lines.append(f"[{prefix}]")
        for k, v in scalars.items():
            lines.append(f"{json.dumps(k)} = {val(v)}")
        for k, v in d.items():
            if isinstance(v, dict):
                table(f"{prefix}.{json.dumps(k)}" if prefix else json.dumps(k), v)
            elif isinstance(v, list) and v and isinstance(v[0], dict):
                for item in v:
                    lines.append(f"[[{json.dumps(k)}]]")
                    for ik, iv in item.items():
                        lines.append(f"{json.dumps(ik)} = {val(iv)}")
    table("", raw)
    return "\n".join(lines) + "\n"


class ConfigTests(unittest.TestCase):
    def test_example_config_parses_and_renders(self):
        cfg = cp.load_config(HERE.parent / "config.example.toml")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.assertTrue(any("abs.api_key_file" in e for e in cfg.secret_errors))
        cfg.abs_api_key = cfg.template_api_key = "k" * 20  # the *_file secrets do not exist here
        cfg.secret_errors = []
        cfg.state_dir = Path(tmp.name) / "state"
        cfg.validate()
        p = cp.Provisioner(cfg, docker=FakeDocker())
        text = p.render_compose({"slug": "wes", "username": "Wes", "apiKey": "0" * 32})
        self.assertIn('"/srv/media/users/wes/ebooks:/books/ebooks"', text)
        self.assertIn('"/srv/downloads:/downloads"', text)
        self.assertIn('name: "audiobookshelf_default"', text)

    def test_validate_reports_missing(self):
        with self.assertRaisesRegex(cp.ConfigError, "abs.url"):
            cp.Config({}).validate()
        with self.assertRaisesRegex(cp.ConfigError, "server.token"):
            cfg = cp.Config({"abs": {"url": "http://a", "api_key": "k"}, "docker": {"network": "n"},
                             "paths": {k: {"host": "/a", "container": "/b", "abs": "/c"} for k in cp.FOLDER_KEYS}})
            cfg.validate(need_server=True)


if __name__ == "__main__":
    unittest.main()
