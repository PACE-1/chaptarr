#!/usr/bin/env python3
"""chaptarr-provision: create and manage one private Chaptarr instance per Audiobookshelf user.

Runs on the Docker host (or in the chaptarr-provisioner image with the Docker socket
mounted). Two front ends share the same engine:

* the CLI (``create``, ``list``, ``status``, ``delete``, ``import-profiles``), and
* ``serve``, a small bearer-token HTTP API that the ABS Chaptarr plugin calls over the
  internal Docker network. It must never be published through a reverse proxy.

Every provisioning step is idempotent and recorded in a per-user state file, so a
failed run can be re-run and resumes where it stopped. Python 3.11+, stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import datetime as _dt
import fcntl
import hmac
import http.server
import json
import logging
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

VERSION = "1.0.0"

LOG = logging.getLogger("chaptarr-provision")

DEFAULT_CONFIG_PATH = "/etc/chaptarr-provision/config.toml"
DEFAULT_IMAGE = "ghcr.io/pace-1/chaptarr:develop"
DEFAULT_LISTEN = "0.0.0.0:8790"
CHAPTARR_PORT = 8789
SECRET_PLACEHOLDER = "********"  # what Chaptarr returns for secret provider fields

STEPS = [
    "validate",
    "abs_user",
    "folders",
    "api_key",
    "container",
    "wait_ready",
    "chaptarr_config",
    "shelfmark",
    "abs_libraries",
    "register",
    "hooks",
]
# Cheap, self-healing steps that run on every create, even after they succeeded once.
ALWAYS_RUN = {"validate", "container", "wait_ready"}

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FOLDER_KEYS = ("config", "ebooks", "audiobooks", "downloads")

# Root folder types, from src/NzbDrone.Core/RootFolders/RootFolder.cs (FolderType enum).
FOLDER_TYPE_AUDIOBOOK = 1
FOLDER_TYPE_EBOOK = 2


# --------------------------------------------------------------------------- errors


class ProvisionError(Exception):
    """A failure that is reported to the operator (message is masked before display)."""


class ValidationError(ProvisionError):
    """Bad input: invalid username/slug, collision, malformed profile JSON."""


class BusyError(ProvisionError):
    """Another run holds the lock for this user."""


class ConfigError(ProvisionError):
    pass


class HttpError(ProvisionError):
    def __init__(self, method: str, url: str, status: int, body: str):
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")


# --------------------------------------------------------------------------- masking


class Masker:
    """Replaces known secrets (and secret-looking key/value pairs) with ``***``."""

    _PATTERNS = [
        re.compile(r'(?i)("(?:apiKey|api_key|password|token|secret|mamId)"\s*:\s*")([^"]+)(")'),
        re.compile(r"(?i)((?:apikey|api_key|password|token)=)([^&\s\"']+)()"),
        re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)()"),
        re.compile(r"(?i)(x-api-key:\s*)(\S+)()"),
    ]

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add(self, *values: Any) -> None:
        with self._lock:
            for value in values:
                if isinstance(value, str) and len(value) >= 4 and value != SECRET_PLACEHOLDER:
                    self._secrets.add(value)

    def mask(self, text: Any) -> str:
        text = "" if text is None else str(text)
        with self._lock:
            known = sorted(self._secrets, key=len, reverse=True)
        for value in known:
            text = text.replace(value, "***")
        for pattern in self._PATTERNS:
            text = pattern.sub(lambda m: m.group(1) + "***" + m.group(3), text)
        return text


class MaskingFilter(logging.Filter):
    def __init__(self, masker: Masker):
        super().__init__()
        self.masker = masker

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.masker.mask(record.getMessage())
        record.args = None
        return True


# --------------------------------------------------------------------------- helpers


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(username: str) -> str:
    """Derive the instance slug (``[a-z0-9-]``) from an ABS username."""
    if not isinstance(username, str) or not username.strip():
        raise ValidationError("username is required")
    slug = re.sub(r"[^a-z0-9]+", "-", username.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:32].strip("-")
    if not slug or not SLUG_RE.match(slug):
        raise ValidationError(f"cannot derive a valid slug from username {username!r}; use letters and digits")
    return slug


def validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise ValidationError(f"invalid slug {slug!r}: expected [a-z0-9-], 1-32 chars")
    return slug


def display_name(username: str) -> str:
    username = username.strip()
    return username[:1].upper() + username[1:]


def render_path(template: str, slug: str, username: str) -> str:
    """Expand ``{slug}`` and ``{username}`` and insist on a clean absolute path."""
    if any(c in username for c in "/\\\0") or username.strip() in (".", ".."):
        raise ValidationError(f"username {username!r} cannot be used in a path")
    value = template.replace("{slug}", slug).replace("{username}", username)
    check_clean_abs_path(value, what=f"path template {template!r}")
    return value


def check_clean_abs_path(value: Any, what: str = "path") -> None:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ValidationError(f"{what}: must be an absolute path")
    if ".." in value.split("/") or "\0" in value:
        raise ValidationError(f"{what}: must not contain '..'")


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    """Write via temp file + fsync + rename so readers never see a partial file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = data.encode() if isinstance(data, str) else data
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def fields_to_dict(resource: dict) -> dict:
    return {f.get("name"): f.get("value") for f in resource.get("fields") or []}


def set_field(resource: dict, name: str, value: Any, create: bool = False) -> bool:
    for field in resource.setdefault("fields", []):
        if field.get("name") == name:
            field["value"] = value
            return True
    if create:
        resource["fields"].append({"name": name, "value": value})
        return True
    return False


def norm_path(path: str) -> str:
    return path.rstrip("/") or "/"


# --------------------------------------------------------------------------- config


def _get(d: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = d
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _secret(section: dict, key: str, errors: list[str] | None = None, where: str = "") -> str | None:
    """Read ``key`` or ``key_file`` (the file's stripped contents) from a config table.

    An unreadable file is recorded in ``errors`` (reported by ``Config.validate``) rather than
    raised, so e.g. a missing server token does not break CLI commands that do not need it.
    """
    if section.get(key):
        return str(section[key])
    file_key = f"{key}_file"
    if section.get(file_key):
        try:
            return Path(section[file_key]).read_text().strip() or None
        except OSError as exc:
            if errors is not None:
                errors.append(f"cannot read {where}{file_key} ({exc.strerror or exc})")
    return None


class Config:
    """Typed view over config.toml. See config.example.toml for every key."""

    def __init__(self, raw: dict, base_dir: Path | None = None):
        self.raw = raw
        base_dir = base_dir or Path.cwd()
        self.state_dir = Path(raw.get("state_dir") or "/var/lib/chaptarr-provision")
        self.stacks_dir = Path(raw.get("stacks_dir") or "/opt/chaptarr-stacks")
        hooks = raw.get("hooks_dir", "/etc/chaptarr-provision/hooks.d")
        self.hooks_dir = Path(hooks) if hooks else None

        server = raw.get("server") or {}
        self.listen = server.get("listen") or DEFAULT_LISTEN
        self.secret_errors: list[str] = []
        self.token_errors: list[str] = []
        self.token = os.environ.get("CHAPTARR_PROVISION_TOKEN") or _secret(server, "token", self.token_errors, "server.")

        abs_ = raw.get("abs") or {}
        self.abs_url = (abs_.get("url") or "").rstrip("/")
        self.abs_api_key = _secret(abs_, "api_key", self.secret_errors, "abs.")
        self.abs_plugin_id = abs_.get("plugin_id") or "chaptarr"
        self.abs_library_provider = abs_.get("library_provider") or "google"
        self.abs_library_icon_ebook = abs_.get("ebook_library_icon") or "books-1"
        self.abs_library_icon_audiobook = abs_.get("audiobook_library_icon") or "audiobookshelf"
        self.abs_user_permissions = abs_.get("user_permissions") or {}

        docker = raw.get("docker") or {}
        self.network = docker.get("network") or ""
        self.image = docker.get("image") or DEFAULT_IMAGE
        self.container_prefix = docker.get("container_prefix") or "chaptarr-"
        self.puid = int(docker.get("puid", 1000))
        self.pgid = int(docker.get("pgid", 1000))
        self.tz = docker.get("tz") or "Etc/UTC"
        self.umask = docker.get("umask")
        self.extra_volumes = list(docker.get("extra_volumes") or [])
        self.extra_env = dict(docker.get("extra_env") or {})
        self.docker_cmd = list(docker.get("docker_command") or ["docker"])
        self.ready_timeout = float(docker.get("ready_timeout", 180))
        self.ready_interval = float(docker.get("ready_interval", 3))
        self.provisioner_url = docker.get("provisioner_url") or ""
        tmpl = docker.get("compose_template")
        default_tmpl = Path(__file__).resolve().parent / "templates" / "compose.yaml.tmpl"
        self.compose_template = (base_dir / tmpl) if tmpl else default_tmpl
        self.force_save = bool(docker.get("force_save", False))

        self.paths = raw.get("paths") or {}

        template = raw.get("template") or {}
        self.template_url = (template.get("url") or "").rstrip("/")
        self.template_api_key = _secret(template, "api_key", self.secret_errors, "template.")
        self.copy_quality_profiles = bool(template.get("copy_quality_profiles", True))
        self.copy_metadata_profiles = bool(template.get("copy_metadata_profiles", True))
        self.copy_indexers = bool(template.get("copy_indexers", True))
        self.profile_names = {
            "ebook_quality": template.get("ebook_quality_profile"),
            "audiobook_quality": template.get("audiobook_quality_profile"),
            "ebook_metadata": template.get("ebook_metadata_profile"),
            "audiobook_metadata": template.get("audiobook_metadata_profile"),
        }
        self.indexer_secrets = dict(raw.get("indexer_secrets") or {})
        self.download_clients = list(raw.get("download_clients") or [])

        prowlarr = raw.get("prowlarr") or {}
        self.prowlarr_url = (prowlarr.get("url") or "").rstrip("/")
        self.prowlarr_api_key = _secret(prowlarr, "api_key", self.secret_errors, "prowlarr.")
        self.prowlarr_implementation = prowlarr.get("implementation") or "Readarr"
        self.prowlarr_self_url = (prowlarr.get("prowlarr_url") or self.prowlarr_url).rstrip("/")
        self.prowlarr_sync_level = prowlarr.get("sync_level") or "fullSync"

        shelfmark = raw.get("shelfmark") or {}
        self.shelfmark_users = {str(k): str(v) for k, v in (shelfmark.get("users") or {}).items()}

        self.http_timeout = float(raw.get("http_timeout", 30))

    # ---- validation

    def validate(self, need_server: bool = False) -> None:
        problems = list(self.secret_errors)
        if need_server:
            problems.extend(self.token_errors)
        if not self.abs_url:
            problems.append("abs.url is required")
        if not self.abs_api_key and not self.secret_errors:
            problems.append("abs.api_key (or abs.api_key_file) is required")
        if not self.network:
            problems.append("docker.network is required (the network the ABS container is on)")
        for key in FOLDER_KEYS:
            entry = self.paths.get(key)
            if not isinstance(entry, dict) or not entry.get("host") or not entry.get("container"):
                problems.append(f"paths.{key}.host and paths.{key}.container are required")
        for key in ("ebooks", "audiobooks"):
            if not _get(self.paths, f"{key}.abs"):
                problems.append(f"paths.{key}.abs is required (path inside the ABS container)")
        for k in self.extra_env:
            if not ENV_KEY_RE.match(str(k)):
                problems.append(f"docker.extra_env key {k!r} is not a valid variable name")
        for i, dc in enumerate(self.download_clients):
            if not dc.get("name"):
                problems.append(f"download_clients[{i}].name is required")
        if bool(self.template_url) != bool(self.template_api_key):
            problems.append("template.url and template.api_key must be set together")
        if bool(self.prowlarr_url) != bool(self.prowlarr_api_key):
            problems.append("prowlarr.url and prowlarr.api_key must be set together")
        if need_server and (not self.token or len(self.token) < 16):
            problems.append("server.token (or token_file / CHAPTARR_PROVISION_TOKEN) must be set, 16+ chars")
        if problems:
            raise ConfigError("invalid config: " + "; ".join(problems))

    def secrets(self) -> list[str]:
        """Every configured secret, so the masker can scrub them from errors and logs."""
        values = [self.abs_api_key, self.template_api_key, self.prowlarr_api_key, self.token]
        for dc in self.download_clients:
            for name, value in (dc.get("fields") or {}).items():
                if isinstance(value, str) and re.search(r"(?i)pass|key|token|secret|cookie", name):
                    values.append(value)
        for table in self.indexer_secrets.values():
            if isinstance(table, dict):
                values.extend(v for v in table.values() if isinstance(v, str))
        return [v for v in values if v]

    # ---- per-user values

    def container_name(self, slug: str) -> str:
        return f"{self.container_prefix}{slug}"

    def instance_url(self, slug: str) -> str:
        """URL the ABS container (and Prowlarr) use: container DNS name on the shared network."""
        return f"http://{self.container_name(slug)}:{CHAPTARR_PORT}"

    def user_paths(self, slug: str, username: str) -> dict:
        out: dict[str, dict[str, str]] = {}
        for key in FOLDER_KEYS:
            entry = self.paths[key]
            out[key] = {side: render_path(entry[side], slug, username)
                        for side in ("host", "container", "abs") if entry.get(side)}
        return out


def load_config(path: str | os.PathLike) -> Config:
    p = Path(path)
    try:
        with open(p, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {p}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"config file {p} is not valid TOML: {exc}") from exc
    return Config(raw, base_dir=p.parent)


# --------------------------------------------------------------------------- HTTP clients


class HttpClient:
    """Small JSON-over-HTTP client. Ignores *_PROXY env vars: every target is internal."""

    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, url: str, body: Any = None, headers: dict | None = None,
                timeout: float | None = None) -> tuple[int, Any]:
        data = None
        hdrs = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        hdrs.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            raise HttpError(method, _strip_query(url), exc.code, raw.decode("utf-8", "replace")) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ProvisionError(f"{method} {_strip_query(url)} failed: {reason}") from None
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw.decode("utf-8", "replace")


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]


class ApiClient:
    def __init__(self, http: HttpClient, base_url: str, headers: dict):
        self.http = http
        self.base = base_url.rstrip("/")
        self.headers = headers

    def call(self, method: str, path: str, body: Any = None, params: dict | None = None, **kw) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self.http.request(method, url, body, self.headers, **kw)[1]

    def get(self, path: str, **kw) -> Any:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any, **kw) -> Any:
        return self.call("POST", path, body, **kw)

    def put(self, path: str, body: Any, **kw) -> Any:
        return self.call("PUT", path, body, **kw)

    def patch(self, path: str, body: Any, **kw) -> Any:
        return self.call("PATCH", path, body, **kw)

    def delete(self, path: str, **kw) -> Any:
        return self.call("DELETE", path, **kw)


def chaptarr_client(http: HttpClient, url: str, api_key: str) -> ApiClient:
    return ApiClient(http, url, {"X-Api-Key": api_key})


def abs_client(http: HttpClient, url: str, api_key: str) -> ApiClient:
    return ApiClient(http, url, {"Authorization": f"Bearer {api_key}"})


# --------------------------------------------------------------------------- docker


class DockerRunner:
    """Runs docker CLI commands. Tests replace this with a fake."""

    def __init__(self, docker_cmd: list[str]):
        self.docker_cmd = docker_cmd

    def run(self, args: list[str], cwd: str | None = None, check: bool = True,
            timeout: float = 600) -> subprocess.CompletedProcess:
        cmd = self.docker_cmd + args
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise ProvisionError(f"docker CLI not found ({self.docker_cmd[0]})") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProvisionError(f"docker {' '.join(args[:3])} timed out") from exc
        if check and proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise ProvisionError(f"docker {' '.join(args[:4])} failed (exit {proc.returncode}): {tail}")
        return proc


# --------------------------------------------------------------------------- profiles


def parse_profiles_input(text: str) -> dict:
    """Accept the plugin's userProfiles in any of the shapes it is found in.

    * the object itself: ``{"<absUserId>": {...}}``
    * the JSON-encoded string the plugin stores: ``"{\\"abc\\": {...}}"``
    * a settings object: ``{"userProfiles": "<json string>" | {...}}``
    """
    try:
        value = json.loads(text)
    except ValueError as exc:
        raise ValidationError(f"profiles input is not JSON: {exc}") from None
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except ValueError as exc:
            raise ValidationError(f"userProfiles string is not JSON: {exc}") from None
    if isinstance(value, dict) and "userProfiles" in value and not _looks_like_profile(value.get("userProfiles")):
        inner = value["userProfiles"]
        value = json.loads(inner) if isinstance(inner, str) and inner.strip() else (inner or {})
    if not isinstance(value, dict):
        raise ValidationError("userProfiles must be a JSON object keyed by ABS user id")
    validate_profiles(value)
    return value


def _looks_like_profile(value: Any) -> bool:
    return isinstance(value, dict) and "url" in value and "apiKey" in value


def validate_profile(user_id: str, profile: Any) -> None:
    where = f"profile {user_id!r}"
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValidationError("profile keys must be non-empty ABS user ids")
    if not isinstance(profile, dict):
        raise ValidationError(f"{where}: must be an object")
    url = profile.get("url")
    if not isinstance(url, str) or not re.match(r"^https?://[^\s/]+", url):
        raise ValidationError(f"{where}: url must be an http(s) URL")
    if not isinstance(profile.get("apiKey"), str) or not profile["apiKey"].strip():
        raise ValidationError(f"{where}: apiKey is required")
    if "ebook" not in profile and "audiobook" not in profile:
        raise ValidationError(f"{where}: needs an ebook and/or audiobook section")
    for media in ("ebook", "audiobook"):
        if media not in profile:
            continue
        section = profile[media]
        if not isinstance(section, dict):
            raise ValidationError(f"{where}.{media}: must be an object")
        check_clean_abs_path(section.get("path"), what=f"{where}.{media}.path")
        for key in ("libraryId", "label"):
            if key in section and not isinstance(section[key], str):
                raise ValidationError(f"{where}.{media}.{key}: must be a string")


def validate_profiles(profiles: dict) -> None:
    urls: dict[str, str] = {}
    keys: dict[str, str] = {}
    for user_id, profile in profiles.items():
        validate_profile(user_id, profile)
        url = profile["url"].rstrip("/").lower()
        if url in urls:
            raise ValidationError(f"profiles {urls[url]!r} and {user_id!r} share the same url")
        urls[url] = user_id
        if profile["apiKey"] in keys:
            raise ValidationError(f"profiles {keys[profile['apiKey']]!r} and {user_id!r} share the same apiKey")
        keys[profile["apiKey"]] = user_id


# --------------------------------------------------------------------------- state store


class StateStore:
    """Per-user JSON state files (0600) + profiles.json, with flock-based locking."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.users_dir = self.root / "users"
        self.locks_dir = self.root / "locks"
        self.profiles_path = self.root / "profiles.json"
        for d in (self.root, self.users_dir, self.locks_dir):
            d.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(PermissionError):
                os.chmod(d, 0o700)

    def path(self, slug: str) -> Path:
        return self.users_dir / f"{validate_slug(slug)}.json"

    def load(self, slug: str) -> dict | None:
        try:
            return json.loads(self.path(slug).read_text())
        except FileNotFoundError:
            return None

    def save(self, state: dict) -> None:
        state["updatedAt"] = utcnow()
        atomic_write(self.path(state["slug"]), json.dumps(state, indent=2, sort_keys=True) + "\n")

    def remove(self, slug: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path(slug).unlink()

    def all(self) -> list[dict]:
        out = []
        for p in sorted(self.users_dir.glob("*.json")):
            with contextlib.suppress(ValueError, OSError):
                out.append(json.loads(p.read_text()))
        return out

    @contextlib.contextmanager
    def lock(self, name: str, blocking: bool = True, timeout: float = 30):
        path = self.locks_dir / f"{name}.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not blocking or time.monotonic() > deadline:
                        raise BusyError(f"{name} is busy (another provisioning run holds the lock)") from None
                    time.sleep(0.05)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def load_profiles(self) -> dict:
        try:
            return json.loads(self.profiles_path.read_text())
        except FileNotFoundError:
            return {}

    def save_profiles(self, profiles: dict) -> None:
        validate_profiles(profiles)
        atomic_write(self.profiles_path, json.dumps(profiles, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- engine


ProgressFn = Callable[[str, str, str | None], None]


class Provisioner:
    def __init__(self, config: Config, http: HttpClient | None = None, docker: DockerRunner | None = None,
                 sleep: Callable[[float], None] = time.sleep, masker: Masker | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = config
        self.http = http or HttpClient(config.http_timeout)
        self.docker = docker or DockerRunner(config.docker_cmd)
        self.sleep = sleep
        self.clock = clock
        self.masker = masker or Masker()
        self.masker.add(*config.secrets())
        self.store = StateStore(config.state_dir)
        for st in self.store.all():
            self.masker.add(st.get("apiKey"))
        for prof in self.store.load_profiles().values():
            if isinstance(prof, dict):
                self.masker.add(prof.get("apiKey"))

    # ---- clients

    def abs(self) -> ApiClient:
        return abs_client(self.http, self.cfg.abs_url, self.cfg.abs_api_key or "")

    def template(self) -> ApiClient | None:
        if not self.cfg.template_url:
            return None
        return chaptarr_client(self.http, self.cfg.template_url, self.cfg.template_api_key or "")

    def provisioner_url(self, slug: str) -> str:
        """URL this process uses to reach the instance (differs from the ABS-facing URL only
        when the provisioner is not on the shared network, e.g. ``inspect`` mode on the host)."""
        tmpl = self.cfg.provisioner_url
        if tmpl == "inspect":
            ip = self._container_ip(slug)
            return f"http://{ip}:{CHAPTARR_PORT}"
        if tmpl:
            return tmpl.replace("{slug}", slug).replace("{container}", self.cfg.container_name(slug)).rstrip("/")
        return self.cfg.instance_url(slug)

    def _container_ip(self, slug: str) -> str:
        fmt = "{{(index .NetworkSettings.Networks %s).IPAddress}}" % json.dumps(self.cfg.network)
        proc = self.docker.run(["inspect", "-f", fmt, self.cfg.container_name(slug)])
        ip = proc.stdout.strip()
        if not ip:
            raise ProvisionError(f"container {self.cfg.container_name(slug)} has no IP on network {self.cfg.network}")
        return ip

    def instance(self, state: dict) -> ApiClient:
        return chaptarr_client(self.http, self.provisioner_url(state["slug"]), state["apiKey"])

    def mask(self, text: Any) -> str:
        return self.masker.mask(text)

    # ---- public operations

    def create(self, username: str, abs_user_id: str | None = None, password: str | None = None,
               progress: ProgressFn | None = None) -> dict:
        progress = progress or (lambda name, state, detail: None)
        if password:
            self.masker.add(password)
        slug = slugify(username)
        if abs_user_id is not None and (not isinstance(abs_user_id, str) or not abs_user_id.strip()):
            raise ValidationError("absUserId must be a non-empty string")

        with self.store.lock("_global"):
            state = self._claim(slug, username, abs_user_id)

        with self.store.lock(slug, blocking=False):
            state = self.store.load(slug) or state
            state["state"] = "running"
            state.pop("error", None)
            self.store.save(state)
            ctx = {"password": password}
            for name in STEPS:
                step = state["steps"].setdefault(name, {"state": "pending"})
                if step.get("state") == "done" and name not in ALWAYS_RUN:
                    progress(name, "done", step.get("detail"))
                    continue
                step.update({"state": "running", "startedAt": utcnow()})
                step.pop("error", None)
                self.store.save(state)
                progress(name, "running", None)
                try:
                    detail = getattr(self, f"_step_{name}")(state, ctx)
                except Exception as exc:  # noqa: BLE001 - every failure is recorded and resumable
                    msg = self.mask(exc if isinstance(exc, ProvisionError) else f"{type(exc).__name__}: {exc}")
                    step.update({"state": "failed", "error": msg, "finishedAt": utcnow()})
                    state.update({"state": "failed", "error": f"{name}: {msg}"})
                    self.store.save(state)
                    progress(name, "failed", msg)
                    LOG.error("create %s: step %s failed: %s", slug, name, msg)
                    raise ProvisionError(f"step {name} failed: {msg}") from None
                step.update({"state": "done", "finishedAt": utcnow()})
                if detail:
                    step["detail"] = self.mask(detail)
                else:
                    step.pop("detail", None)
                self.store.save(state)
                progress(name, "done", step.get("detail"))
                LOG.info("create %s: %s done%s", slug, name, f" ({step['detail']})" if detail else "")
            state["state"] = "provisioned"
            state.pop("error", None)
            state.setdefault("provisionedAt", utcnow())
            self.store.save(state)
            return self.result(state)

    def _claim(self, slug: str, username: str, abs_user_id: str | None) -> dict:
        """Under the global lock: detect slug/user collisions and create the state stub."""
        state = self.store.load(slug)
        if state and state.get("username", "").lower() != username.strip().lower():
            raise ValidationError(f"slug {slug!r} is already used by ABS user {state.get('username')!r}")
        if state and abs_user_id and state.get("absUserId") and state["absUserId"] != abs_user_id:
            raise ValidationError(f"{username!r} is already provisioned for a different ABS user id")
        if abs_user_id:
            for other in self.store.all():
                if other.get("slug") != slug and other.get("absUserId") == abs_user_id:
                    raise ValidationError(f"ABS user {abs_user_id!r} already has instance {other.get('slug')!r}")
        profiles = self.store.load_profiles()
        target_url = self.cfg.instance_url(slug).lower()
        own_id = (state or {}).get("absUserId") or abs_user_id
        for uid, prof in profiles.items():
            ours = str(prof.get("url", "")).rstrip("/").lower() == target_url
            if uid == own_id and ours:
                continue  # registered by an earlier run of this provisioner
            if uid == own_id:
                raise ValidationError(f"ABS user {uid!r} already has a Chaptarr profile ({prof.get('url')}) that this "
                                      "provisioner did not create; remove it from profiles first")
            if ours:
                raise ValidationError(f"url {target_url} is already used by the profile of ABS user {uid!r}")
        if state is None:
            state = {
                "slug": slug,
                "username": username.strip(),
                "absUserId": abs_user_id,
                "createdAt": utcnow(),
                "state": "pending",
                "steps": {},
                "ids": {},
            }
        elif abs_user_id and not state.get("absUserId"):
            state["absUserId"] = abs_user_id
        self.store.save(state)
        return state

    def result(self, state: dict) -> dict:
        """Public, secret-free description of a provisioned instance (instance URL is not secret)."""
        paths = self.cfg.user_paths(state["slug"], state["username"])
        ids = state.get("ids", {})
        name = display_name(state["username"])
        return {
            "absUserId": state.get("absUserId"),
            "username": state["username"],
            "slug": state["slug"],
            "url": self.cfg.instance_url(state["slug"]),
            "container": self.cfg.container_name(state["slug"]),
            "ebook": {"path": paths["ebooks"]["container"], "libraryId": ids.get("absEbookLibraryId"),
                      "label": f"{name} Ebooks"},
            "audiobook": {"path": paths["audiobooks"]["container"], "libraryId": ids.get("absAudiobookLibraryId"),
                          "label": f"{name} Audiobooks"},
        }

    def public_state(self, state: dict) -> dict:
        out = copy.deepcopy(state)
        out.pop("apiKey", None)
        if out.get("error"):
            out["error"] = self.mask(out["error"])
        for step in out.get("steps", {}).values():
            if step.get("error"):
                step["error"] = self.mask(step["error"])
        out["url"] = self.cfg.instance_url(state["slug"])
        return out

    def list_users(self) -> list[dict]:
        rows = []
        known_ids = set()
        for st in self.store.all():
            row = {
                "absUserId": st.get("absUserId"),
                "username": st.get("username"),
                "slug": st.get("slug"),
                "state": st.get("state"),
                "url": self.cfg.instance_url(st["slug"]),
                "createdAt": st.get("createdAt"),
            }
            if st.get("error") and st.get("state") == "failed":
                row["error"] = self.mask(st["error"])
            rows.append(row)
            if st.get("absUserId"):
                known_ids.add(st["absUserId"])
        for uid, prof in sorted(self.store.load_profiles().items()):
            if uid in known_ids:
                continue
            host = urllib.parse.urlparse(prof.get("url", "")).hostname or ""
            prefix = self.cfg.container_prefix
            slug = host[len(prefix):] if host.startswith(prefix) and SLUG_RE.match(host[len(prefix):]) else None
            rows.append({"absUserId": uid, "username": None, "slug": slug, "state": "imported",
                         "url": prof.get("url"), "createdAt": None})
        return rows

    def status(self, slug: str) -> dict:
        state = self.store.load(validate_slug(slug))
        if not state:
            raise ValidationError(f"no state for slug {slug!r}")
        return self.public_state(state)

    def import_profiles(self, text: str) -> dict:
        for prof in parse_profiles_input(text).values():
            self.masker.add(prof.get("apiKey"))
        return import_profiles_into(self.store, text)

    def delete(self, slug: str, purge: bool = False) -> dict:
        validate_slug(slug)
        with self.store.lock(slug, blocking=False):
            state = self.store.load(slug)
            if not state:
                raise ValidationError(f"no state for slug {slug!r}")
            stack = self._stack_dir(slug)
            compose = stack / "compose.yaml"
            if compose.exists():
                self.docker.run(["compose", "-f", str(compose), "-p", self.cfg.container_name(slug),
                                 "down", "--remove-orphans"], cwd=str(stack))
            else:
                proc = self.docker.run(["rm", "-f", self.cfg.container_name(slug)], check=False)
                if proc.returncode != 0 and "No such container" not in (proc.stderr or ""):
                    raise ProvisionError(f"docker rm failed: {(proc.stderr or '').strip()[-300:]}")
            removed_paths: list[str] = []
            deregister_error = None
            try:
                deregistered = self._deregister(state)
            except ProvisionError as exc:
                deregistered = False
                deregister_error = self.mask(exc)
            if purge:
                paths = self.cfg.user_paths(slug, state["username"])
                for key in FOLDER_KEYS:
                    host = paths[key]["host"]
                    if self._safe_to_purge(host, slug, state["username"]) and os.path.isdir(host):
                        shutil.rmtree(host)
                        removed_paths.append(host)
                if stack.is_dir() and self._safe_to_purge(str(stack), slug, state["username"]):
                    shutil.rmtree(stack)
                    removed_paths.append(str(stack))
                self.store.remove(slug)
            else:
                for name in ("container", "wait_ready", "register", "hooks"):
                    state["steps"].pop(name, None)
                state["state"] = "deleted"
                state["deletedAt"] = utcnow()
                self.store.save(state)
            out = {"slug": slug, "purged": purge, "deregistered": deregistered, "removedPaths": removed_paths}
            if deregister_error:
                out["error"] = f"container removed, but the ABS profile was not updated: {deregister_error}"
            return out

    def _safe_to_purge(self, path: str, slug: str, username: str) -> bool:
        p = os.path.normpath(path)
        parts = [x for x in p.split("/") if x]
        if len(parts) < 2:
            return False
        return any(slug in part or username in part for part in parts)

    def _deregister(self, state: dict) -> bool:
        uid = state.get("absUserId")
        url = self.cfg.instance_url(state["slug"]).lower()
        with self.store.lock("_profiles"):
            profiles = self.store.load_profiles()
            keys = [k for k, v in profiles.items()
                    if k == uid or str(v.get("url", "")).rstrip("/").lower() == url]
            if not keys:
                return False
            for k in keys:
                profiles.pop(k)
            self.store.save_profiles(profiles)
            self._push_profiles(profiles)
        return True

    def _push_profiles(self, profiles: dict) -> None:
        self.abs().patch(f"/api/plugins/{self.cfg.abs_plugin_id}/settings",
                         {"userProfiles": json.dumps(profiles, sort_keys=True)})

    def _stack_dir(self, slug: str) -> Path:
        return self.cfg.stacks_dir / self.cfg.container_name(slug)

    # ---- steps

    def _step_validate(self, state: dict, ctx: dict) -> str:
        self.cfg.user_paths(state["slug"], state["username"])  # path templates render cleanly
        self.docker.run(["version", "--format", "{{.Server.Version}}"])
        self.docker.run(["network", "inspect", self.cfg.network, "--format", "{{.Name}}"])
        me = self.abs().get("/api/me") or {}
        if me.get("type") not in ("admin", "root"):
            raise ProvisionError("abs.api_key must belong to an ABS admin or root user")
        if not self.cfg.compose_template.is_file():
            raise ProvisionError(f"compose template not found: {self.cfg.compose_template}")
        tmpl = self.template()
        if tmpl:
            tmpl.get("/api/v1/system/status")
        return f"docker ok, ABS ok, template {'ok' if tmpl else 'not configured'}"

    def _step_abs_user(self, state: dict, ctx: dict) -> str:
        abs_ = self.abs()
        if state.get("absUserId"):
            try:
                user = abs_.get(f"/api/users/{urllib.parse.quote(state['absUserId'])}")
            except HttpError as exc:
                if exc.status == 404:
                    raise ProvisionError(f"ABS user {state['absUserId']} does not exist") from None
                raise
            user = user.get("user", user) if isinstance(user, dict) else {}
            state["absUsername"] = user.get("username")
            return f"using existing ABS user {user.get('username')}"
        users = (abs_.get("/api/users") or {}).get("users", [])
        existing = next((u for u in users if str(u.get("username", "")).lower() == state["username"].lower()), None)
        if existing:
            if not state.get("absUserCreateAttempted"):
                raise ValidationError(f"ABS user {state['username']!r} already exists; pass its absUserId instead")
            state["absUserId"] = existing["id"]
            state["absUsername"] = existing.get("username")
            return f"adopted ABS user {existing.get('username')} created by an earlier run"
        password = ctx.get("password")
        if not password:
            raise ValidationError("password is required to create the ABS user")
        perms = {
            "download": True,
            "update": False,
            "delete": False,
            "upload": False,
            "accessAllLibraries": False,
            "accessAllTags": True,
            "accessExplicitContent": True,
        }
        perms.update(self.cfg.abs_user_permissions)
        perms["accessAllLibraries"] = False
        state["absUserCreateAttempted"] = True
        self.store.save(state)
        created = abs_.post("/api/users", {
            "username": state["username"],
            "password": password,
            "type": "user",
            "isActive": True,
            "permissions": perms,
            "librariesAccessible": [],
        })
        user = created.get("user", created) if isinstance(created, dict) else {}
        if not user.get("id"):
            raise ProvisionError("ABS did not return the new user id")
        state["absUserId"] = user["id"]
        state["absUsername"] = user.get("username", state["username"])
        state["absUserCreated"] = True
        return f"created ABS user {state['absUsername']}"

    def _step_folders(self, state: dict, ctx: dict) -> str:
        paths = self.cfg.user_paths(state["slug"], state["username"])
        made = []
        dirs = [paths[k]["host"] for k in ("config", "ebooks", "audiobooks")]
        dirs += [os.path.join(paths["downloads"]["host"], sub) for sub in ("ebook", "audiobook")]
        for d in dirs:
            os.makedirs(d, exist_ok=True)
            made.append(d)
        chown = os.geteuid() == 0
        if chown:
            for d in dirs + [paths["downloads"]["host"]]:
                os.chown(d, self.cfg.puid, self.cfg.pgid)
        return f"{len(made)} folders ready" + (f", owned by {self.cfg.puid}:{self.cfg.pgid}" if chown else "")

    def _step_api_key(self, state: dict, ctx: dict) -> str:
        if not state.get("apiKey"):
            state["apiKey"] = secrets.token_hex(16)
        self.masker.add(state["apiKey"])
        return "api key stored"

    def render_compose(self, state: dict) -> str:
        slug, username = state["slug"], state["username"]
        paths = self.cfg.user_paths(slug, username)
        volumes = [f"{paths[k]['host']}:{paths[k]['container']}" for k in FOLDER_KEYS]
        for vol in self.cfg.extra_volumes:
            spec = str(vol).replace("{slug}", slug).replace("{username}", username)
            check_clean_abs_path(spec.split(":", 1)[0], what=f"docker.extra_volumes entry {vol!r}")
            volumes.append(spec)
        env = {"PUID": str(self.cfg.puid), "PGID": str(self.cfg.pgid), "TZ": self.cfg.tz}
        if self.cfg.umask:
            env["UMASK"] = str(self.cfg.umask)
        for k, v in self.cfg.extra_env.items():
            env[str(k)] = str(v).replace("{slug}", slug).replace("{username}", username)
        values = {
            "slug": slug,
            "username_comment": re.sub(r"[^\w.@ -]", "_", username),
            "project": json.dumps(self.cfg.container_name(slug)),
            "container_name": json.dumps(self.cfg.container_name(slug)),
            "image": json.dumps(self.cfg.image),
            "network": json.dumps(self.cfg.network),
            "environment": "\n".join(f"      {k}: {json.dumps(v)}" for k, v in env.items()),
            "volumes": "\n".join(f"      - {json.dumps(v)}" for v in volumes),
            "slug_label": json.dumps(slug),
        }
        text = self.cfg.compose_template.read_text()

        def sub(m: re.Match) -> str:
            key = m.group(1)
            if key not in values:
                raise ProvisionError(f"unknown placeholder {{{{{key}}}}} in compose template")
            return values[key]

        return re.sub(r"\{\{\s*([a-z_]+)\s*\}\}", sub, text)

    def _step_container(self, state: dict, ctx: dict) -> str:
        stack = self._stack_dir(state["slug"])
        stack.mkdir(parents=True, exist_ok=True)
        compose = stack / "compose.yaml"
        atomic_write(compose, self.render_compose(state), mode=0o644)
        # The key lives only in .env (0600); compose interpolates ${CHAPTARR_API_KEY}.
        atomic_write(stack / ".env", f"CHAPTARR_API_KEY={state['apiKey']}\n", mode=0o600)
        self.docker.run(["compose", "-f", str(compose), "-p", self.cfg.container_name(state["slug"]),
                         "up", "-d", "--remove-orphans"], cwd=str(stack))
        return f"{self.cfg.container_name(state['slug'])} up ({self.cfg.image})"

    def _step_wait_ready(self, state: dict, ctx: dict) -> str:
        deadline = self.clock() + self.cfg.ready_timeout
        last = "no response"
        while True:
            try:
                status = self.instance(state).get("/api/v1/system/status", timeout=10)
                return f"Chaptarr {status.get('version', '?') if isinstance(status, dict) else '?'} ready"
            except HttpError as exc:
                if exc.status == 401:
                    raise ProvisionError("instance rejected the API key (is Chaptarr__Auth__ApiKey applied?)") from None
                last = f"HTTP {exc.status}"
            except ProvisionError as exc:
                last = str(exc)
            if self.clock() >= deadline:
                raise ProvisionError(f"instance not ready after {int(self.cfg.ready_timeout)}s: {last}")
            self.sleep(self.cfg.ready_interval)

    # ---- chaptarr configuration

    def _step_chaptarr_config(self, state: dict, ctx: dict) -> str:
        inst = self.instance(state)
        tmpl = self.template()
        ids = state.setdefault("ids", {})
        notes = []
        if tmpl and self.cfg.copy_quality_profiles:
            notes.append(f"quality profiles +{self._copy_quality_profiles(tmpl, inst)}")
        if tmpl and self.cfg.copy_metadata_profiles:
            notes.append(f"metadata profiles +{self._copy_metadata_profiles(tmpl, inst)}")
        ids.update(self._pick_profiles(inst))
        self.store.save(state)
        notes.append(f"root folders +{self._root_folders(state, inst)}")
        self.store.save(state)
        notes.append(f"download clients +{self._download_clients(state, inst, tmpl)}")
        self.store.save(state)
        notes.append(f"remote path mappings +{self._remote_path_mappings(state, inst)}")
        if self.cfg.prowlarr_url:
            notes.append("prowlarr app " + self._prowlarr_app(state))
        elif tmpl and self.cfg.copy_indexers:
            notes.append(f"indexers +{self._copy_indexers(tmpl, inst)}")
        return ", ".join(notes)

    @staticmethod
    def _media_of(profile_type: Any) -> str | None:
        value = str(profile_type).lower()
        return {"1": "audiobook", "audiobook": "audiobook", "2": "ebook", "ebook": "ebook"}.get(value)

    def _copy_quality_profiles(self, tmpl: ApiClient, inst: ApiClient) -> int:
        existing = {p["name"].lower() for p in inst.get("/api/v1/qualityprofile") or []}
        added = 0
        for prof in tmpl.get("/api/v1/qualityprofile") or []:
            if prof["name"].lower() in existing:
                continue
            media = self._media_of(prof.get("profileType")) or "audiobook"
            schema = inst.get("/api/v1/qualityprofile/schema", params={"mediaType": media}) or {}
            # Custom format ids differ per instance: every applicable format must be listed,
            # so take the instance's list and carry scores across by built-in key or name.
            scores = {}
            for item in prof.get("formatItems") or []:
                if item.get("builtInKey"):
                    scores[("k", item["builtInKey"])] = item.get("score", 0)
                scores[("n", str(item.get("name", "")).lower())] = item.get("score", 0)
            format_items = []
            for item in schema.get("formatItems") or []:
                by_name = scores.get(("n", str(item.get("name", "")).lower()), item.get("score", 0))
                score = scores.get(("k", item.get("builtInKey")), by_name)
                format_items.append({**item, "score": score})
            body = {k: v for k, v in prof.items() if k != "id"}
            body["formatItems"] = format_items
            if format_items:
                best = sum(max(0, i["score"]) for i in format_items)
                body["minFormatScore"] = min(body.get("minFormatScore", 0), best)
            else:
                body["minFormatScore"] = min(body.get("minFormatScore", 0), 0)
            inst.post("/api/v1/qualityprofile", body)
            existing.add(prof["name"].lower())
            added += 1
        return added

    def _copy_metadata_profiles(self, tmpl: ApiClient, inst: ApiClient) -> int:
        existing = {p["name"].lower() for p in inst.get("/api/v1/metadataprofile") or []}
        added = 0
        for prof in tmpl.get("/api/v1/metadataprofile") or []:
            if prof["name"].lower() in existing or prof["name"] == "None":
                continue
            inst.post("/api/v1/metadataprofile", {k: v for k, v in prof.items() if k != "id"})
            existing.add(prof["name"].lower())
            added += 1
        return added

    def _pick_profiles(self, inst: ApiClient) -> dict:
        qps = inst.get("/api/v1/qualityprofile") or []
        mps = [p for p in inst.get("/api/v1/metadataprofile") or [] if p.get("name") != "None"]
        out = {}
        for media in ("ebook", "audiobook"):
            out[f"{media}QualityProfileId"] = self._pick(qps, self.cfg.profile_names[f"{media}_quality"], media,
                                                         f"{media} quality profile")
            out[f"{media}MetadataProfileId"] = self._pick(mps, self.cfg.profile_names[f"{media}_metadata"], media,
                                                          f"{media} metadata profile", allow_general=True)
        return out

    def _pick(self, profiles: list, wanted: str | None, media: str, what: str, allow_general: bool = False) -> int:
        if wanted:
            for p in profiles:
                if p.get("name", "").lower() == wanted.lower():
                    return p["id"]
            raise ProvisionError(f"{what} {wanted!r} not found on the instance")
        for p in profiles:
            if self._media_of(p.get("profileType")) == media:
                return p["id"]
        if allow_general:
            for p in profiles:
                if str(p.get("profileType")).lower() in ("0", "general"):
                    return p["id"]
        if profiles:
            return profiles[0]["id"]
        raise ProvisionError(f"no {what} exists on the instance")

    def _root_folders(self, state: dict, inst: ApiClient) -> int:
        paths = self.cfg.user_paths(state["slug"], state["username"])
        ids = state["ids"]
        name = display_name(state["username"])
        existing = {norm_path(r.get("path", "")): r for r in inst.get("/api/v1/rootfolder") or []}
        added = 0
        for media, key, folder_type in (("ebook", "ebooks", FOLDER_TYPE_EBOOK),
                                        ("audiobook", "audiobooks", FOLDER_TYPE_AUDIOBOOK)):
            path = paths[key]["container"]
            found = existing.get(norm_path(path))
            if found:
                ids[f"{media}RootFolderId"] = found["id"]
                continue
            body = {
                "name": f"{name} {'Ebooks' if media == 'ebook' else 'Audiobooks'}",
                "path": path,
                "folderType": folder_type,
                "defaultTags": [],
                media: {
                    "qualityProfileId": ids[f"{media}QualityProfileId"],
                    "metadataProfileId": ids[f"{media}MetadataProfileId"],
                },
            }
            created = inst.post("/api/v1/rootfolder", body)
            ids[f"{media}RootFolderId"] = (created or {}).get("id")
            added += 1
        return added

    def _categories(self, dc: dict, slug: str) -> dict:
        ebook = dc.get("ebook_category", "chaptarr-{slug}-ebook")
        audio = dc.get("audiobook_category", "chaptarr-{slug}-audiobook")
        return {"ebookCategory": ebook.replace("{slug}", slug) if ebook else ebook,
                "audiobookCategory": audio.replace("{slug}", slug) if audio else audio}

    def build_download_client(self, dc: dict, slug: str, template_clients: list, schema: list) -> dict:
        name = dc["name"].replace("{slug}", slug)
        base = None
        if dc.get("from_template", True):
            wanted = (dc.get("template_name") or dc["name"]).lower()
            base = next((c for c in template_clients if c.get("name", "").lower() == wanted), None)
        if base is None:
            impl = dc.get("implementation")
            if not impl:
                raise ProvisionError(f"download client {name!r}: not found on the template instance, so "
                                     "'implementation' is required")
            base = next((s for s in schema if s.get("implementation", "").lower() == impl.lower()), None)
            if base is None:
                raise ProvisionError(f"download client {name!r}: unknown implementation {impl!r}")
        body = copy.deepcopy(base)
        body.pop("id", None)
        body["name"] = name
        for key in ("tags", "audiobookTags", "ebookTags"):  # tag ids are per instance
            if key in body:
                body[key] = []
        for key in ("enable", "priority", "removeCompletedDownloads", "removeFailedDownloads"):
            if key in dc:
                body[key] = dc[key]
        known = set(fields_to_dict(body))
        for fname, value in (dc.get("fields") or {}).items():
            if fname not in known:
                raise ProvisionError(f"download client {name!r}: no field named {fname!r} (known: {', '.join(sorted(known))})")
            set_field(body, fname, value)
        for fname, value in self._categories(dc, slug).items():
            if fname in known and value is not None:
                set_field(body, fname, value)
        masked = [f["name"] for f in body.get("fields", []) if f.get("value") == SECRET_PLACEHOLDER]
        if masked:
            raise ProvisionError(f"download client {name!r}: secret field(s) {', '.join(masked)} come back masked "
                                 "from the template; set them under [download_clients.fields] in the config")
        return body

    def _download_clients(self, state: dict, inst: ApiClient, tmpl: ApiClient | None) -> int:
        if not self.cfg.download_clients:
            return 0
        ids = state["ids"].setdefault("downloadClients", {})
        existing = {c["name"].lower(): c for c in inst.get("/api/v1/downloadclient") or []}
        template_clients = (tmpl.get("/api/v1/downloadclient") or []) if tmpl else []
        schema = None
        added = 0
        for dc in self.cfg.download_clients:
            name = dc["name"].replace("{slug}", state["slug"])
            if name.lower() in existing:
                ids[name] = existing[name.lower()]["id"]
                continue
            if schema is None:
                schema = inst.get("/api/v1/downloadclient/schema") or []
            body = self.build_download_client(dc, state["slug"], template_clients, schema)
            params = {"forceSave": "true"} if self.cfg.force_save else None
            created = inst.post("/api/v1/downloadclient", body, params=params) or {}
            ids[name] = created.get("id")
            added += 1
        return added

    def _remote_path_mappings(self, state: dict, inst: ApiClient) -> int:
        slug, username = state["slug"], state["username"]
        existing = {(m.get("downloadClientId"), norm_path(m.get("remotePath", ""))) for m in
                    inst.get("/api/v1/remotepathmapping") or []}
        client_ids = state["ids"].get("downloadClients", {})
        hosts = {c.get("id"): fields_to_dict(c).get("host") or "" for c in inst.get("/api/v1/downloadclient") or []}
        added = 0
        for dc in self.cfg.download_clients:
            name = dc["name"].replace("{slug}", slug)
            for rpm in dc.get("remote_path_mappings") or []:
                client_id = client_ids.get(name)
                if not client_id:
                    raise ProvisionError(f"remote path mapping for {name!r}: download client was not created")
                remote = rpm["remote_path"].replace("{slug}", slug).replace("{username}", username)
                local = render_path(rpm["local_path"], slug, username)
                if (client_id, norm_path(remote)) in existing:
                    continue
                host = rpm.get("host") or hosts.get(client_id) or ""
                inst.post("/api/v1/remotepathmapping", {
                    "downloadClientId": client_id,
                    "host": host,
                    "remotePath": remote if remote.endswith("/") else remote + "/",
                    "localPath": local if local.endswith("/") else local + "/",
                })
                existing.add((client_id, norm_path(remote)))
                added += 1
        return added

    def _copy_indexers(self, tmpl: ApiClient, inst: ApiClient) -> int:
        existing = {i["name"].lower() for i in inst.get("/api/v1/indexer") or []}
        added = 0
        for idx in tmpl.get("/api/v1/indexer") or []:
            if idx["name"].lower() in existing:
                continue
            body = copy.deepcopy(idx)
            body.pop("id", None)
            for key in ("tags", "audiobookTags", "ebookTags"):
                if key in body:
                    body[key] = []
            if "downloadClientId" in body:
                body["downloadClientId"] = 0
            overrides = self.cfg.indexer_secrets.get(idx["name"]) or {}
            for fname, value in overrides.items():
                if not set_field(body, fname, value):
                    raise ProvisionError(f"indexer {idx['name']!r}: no field named {fname!r}")
            masked = [f["name"] for f in body.get("fields", []) if f.get("value") == SECRET_PLACEHOLDER]
            if masked:
                raise ProvisionError(f"indexer {idx['name']!r}: secret field(s) {', '.join(masked)} come back masked; "
                                     f"set them under [indexer_secrets.\"{idx['name']}\"] in the config")
            params = {"forceSave": "true"} if self.cfg.force_save else None
            inst.post("/api/v1/indexer", body, params=params)
            existing.add(idx["name"].lower())
            added += 1
        return added

    def _prowlarr_app(self, state: dict) -> str:
        pr = chaptarr_client(self.http, self.cfg.prowlarr_url, self.cfg.prowlarr_api_key or "")
        name = f"Chaptarr ({state['slug']})"
        base_url = self.cfg.instance_url(state["slug"])
        for app in pr.get("/api/v1/applications") or []:
            if app.get("name") == name or fields_to_dict(app).get("baseUrl", "").rstrip("/") == base_url:
                state["ids"]["prowlarrAppId"] = app.get("id")
                return "exists"
        schema = pr.get("/api/v1/applications/schema") or []
        impl = self.cfg.prowlarr_implementation
        base = next((s for s in schema if s.get("implementation", "").lower() == impl.lower()), None)
        if base is None:
            raise ProvisionError(f"Prowlarr has no application implementation {impl!r}")
        body = copy.deepcopy(base)
        body.pop("id", None)
        body["name"] = name
        body["syncLevel"] = self.cfg.prowlarr_sync_level
        set_field(body, "prowlarrUrl", self.cfg.prowlarr_self_url)
        set_field(body, "baseUrl", base_url)
        set_field(body, "apiKey", state["apiKey"])
        params = {"forceSave": "true"} if self.cfg.force_save else None
        created = pr.post("/api/v1/applications", body, params=params) or {}
        state["ids"]["prowlarrAppId"] = created.get("id")
        return "added"

    # ---- shelfmark / hooks

    def shelfmark_user_for(self, state: dict) -> str | None:
        users = self.cfg.shelfmark_users
        for key in (state.get("absUserId"), state.get("username"), (state.get("username") or "").lower(),
                    state.get("absUsername")):
            if key and key in users:
                return users[key]
        return None

    def _step_shelfmark(self, state: dict, ctx: dict) -> str:
        return apply_shelfmark_user(self.instance(state), self.shelfmark_user_for(state))

    def _step_abs_libraries(self, state: dict, ctx: dict) -> str:
        abs_ = self.abs()
        paths = self.cfg.user_paths(state["slug"], state["username"])
        ids = state["ids"]
        name = display_name(state["username"])
        libs = (abs_.get("/api/libraries") or {}).get("libraries", [])
        notes = []
        for media, key, label, icon in (
            ("ebook", "ebooks", f"{name} Ebooks", self.cfg.abs_library_icon_ebook),
            ("audiobook", "audiobooks", f"{name} Audiobooks", self.cfg.abs_library_icon_audiobook),
        ):
            id_key = f"abs{media.capitalize()}LibraryId"
            full_path = paths[key]["abs"]
            known = ids.get(id_key)
            found = next((lib for lib in libs if lib.get("id") == known), None) if known else None
            if found is None:
                found = next((lib for lib in libs if any(norm_path(f.get("fullPath", "")) == norm_path(full_path)
                                                         for f in lib.get("folders") or [])), None)
            if found is None:
                created = abs_.post("/api/libraries", {
                    "name": label,
                    "folders": [{"fullPath": full_path}],
                    "icon": icon,
                    "mediaType": "book",
                    "provider": self.cfg.abs_library_provider,
                })
                found = created.get("library", created) if isinstance(created, dict) else {}
                if not found.get("id"):
                    raise ProvisionError(f"ABS did not return an id for library {label!r}")
                notes.append(f"created {label}")
            ids[id_key] = found["id"]
            self.store.save(state)
        user = abs_.get(f"/api/users/{urllib.parse.quote(state['absUserId'])}") or {}
        user = user.get("user", user)
        current = list(user.get("librariesAccessible") or [])
        wanted = [ids["absEbookLibraryId"], ids["absAudiobookLibraryId"]]
        if (user.get("permissions") or {}).get("accessAllLibraries"):
            notes.append("user has access to all libraries; left unchanged")
        elif not all(w in current for w in wanted):
            abs_.patch(f"/api/users/{urllib.parse.quote(state['absUserId'])}",
                       {"librariesAccessible": current + [w for w in wanted if w not in current]})
            notes.append("granted access")
        return ", ".join(notes) or "libraries already present"

    def profile_for(self, state: dict) -> dict:
        res = self.result(state)
        return {
            "url": res["url"],
            "apiKey": state["apiKey"],
            "ebook": res["ebook"],
            "audiobook": res["audiobook"],
        }

    def _step_register(self, state: dict, ctx: dict) -> str:
        uid = state["absUserId"]
        profile = self.profile_for(state)
        with self.store.lock("_profiles"):
            profiles = self.store.load_profiles()
            merged = dict(profiles)
            merged[uid] = profile
            validate_profiles(merged)  # unique url/apiKey across every user, clean paths
            self.store.save_profiles(merged)
            self._push_profiles(merged)
        return f"profile registered ({len(merged)} users)"

    def _step_hooks(self, state: dict, ctx: dict) -> str:
        return run_hooks(self.cfg.hooks_dir, self.result(state), self.mask)


def apply_shelfmark_user(client: ApiClient, shelfmark_user_id: str | None) -> str:
    """Point the instance's Shelfmark download client at the user's Shelfmark account.

    The ``Shelfmark`` download client implementation and its ``shelfmarkUserId`` field come
    from the native Shelfmark client that is being built separately in Chaptarr. Until an
    instance has such a client (or no Shelfmark user id is mapped for this ABS user in
    ``[shelfmark.users]``) this does nothing and reports why.
    """
    if not shelfmark_user_id:
        return "skipped: no Shelfmark user mapped"
    clients = [c for c in client.get("/api/v1/downloadclient") or [] if c.get("implementation") == "Shelfmark"]
    if not clients:
        return "skipped: no Shelfmark download client"
    changed = 0
    for c in clients:
        if fields_to_dict(c).get("shelfmarkUserId") == shelfmark_user_id:
            continue
        set_field(c, "shelfmarkUserId", shelfmark_user_id, create=True)
        # forceSave: secret fields come back as the placeholder, which the API keeps as-is.
        client.put(f"/api/v1/downloadclient/{c['id']}", c, params={"forceSave": "true"})
        changed += 1
    return f"shelfmarkUserId set on {changed} client(s)" if changed else "shelfmarkUserId already set"


def run_hooks(hooks_dir: Path | None, result: dict, mask: Callable[[Any], str], timeout: float = 120) -> str:
    """Run every executable in ``hooks_dir`` (sorted) with the result JSON on stdin.

    Hook failures are reported in the step detail but do not fail provisioning.
    """
    if not hooks_dir or not hooks_dir.is_dir():
        return "no hooks"
    payload = json.dumps(result, sort_keys=True)
    notes = []
    for hook in sorted(hooks_dir.iterdir()):
        if not hook.is_file() or not os.access(hook, os.X_OK) or hook.name.startswith("."):
            continue
        try:
            proc = subprocess.run([str(hook)], input=payload, capture_output=True, text=True, timeout=timeout)
            if proc.returncode == 0:
                notes.append(f"{hook.name} ok")
            else:
                notes.append(f"{hook.name} exit {proc.returncode}: {mask((proc.stderr or '').strip()[-200:])}")
                LOG.warning("hook %s exited %s", hook.name, proc.returncode)
        except (OSError, subprocess.TimeoutExpired) as exc:
            notes.append(f"{hook.name} failed: {mask(exc)}")
    return ", ".join(notes) or "no hooks"


# --------------------------------------------------------------------------- serve


class JobManager:
    """Runs create jobs one at a time on a background thread; tracks progress in memory."""

    def __init__(self, provisioner: Provisioner):
        self.p = provisioner
        self.jobs: dict[str, dict] = {}
        self.by_username: dict[str, str] = {}
        self.lock = threading.Lock()
        self.queue: queue.Queue = queue.Queue()
        self.thread = threading.Thread(target=self._worker, name="provision-worker", daemon=True)
        self.thread.start()

    def submit(self, username: str, abs_user_id: str | None, password: str | None) -> tuple[dict, bool]:
        slug = slugify(username)
        key = username.strip().lower()
        with self.lock:
            existing_id = self.by_username.get(key)
            if existing_id and self.jobs[existing_id]["state"] in ("queued", "running"):
                return self.jobs[existing_id], False
            state = self.p.store.load(slug) or {}
            steps = []
            for name in STEPS:
                st = (state.get("steps") or {}).get(name, {})
                steps.append({"name": name, "state": "done" if st.get("state") == "done" else "pending"})
            job = {"jobId": uuid.uuid4().hex, "state": "queued", "username": username.strip(), "slug": slug,
                   "steps": steps, "createdAt": utcnow()}
            self.jobs[job["jobId"]] = job
            self.by_username[key] = job["jobId"]
        self.queue.put((job["jobId"], username, abs_user_id, password))
        return job, True

    def active_for_slug(self, slug: str) -> bool:
        with self.lock:
            return any(j["slug"] == slug and j["state"] in ("queued", "running") for j in self.jobs.values())

    def last_by_slug(self) -> dict[str, str]:
        """Most recent job id per slug, so a UI can reattach to progress after a reload."""
        with self.lock:
            return {j["slug"]: jid for jid, j in sorted(self.jobs.items(), key=lambda kv: kv[1]["createdAt"])}

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return copy.deepcopy(job) if job else None

    def _worker(self) -> None:
        while True:
            job_id, username, abs_user_id, password = self.queue.get()
            job = self.jobs[job_id]

            def progress(name: str, st: str, detail: str | None, job: dict = job) -> None:
                with self.lock:
                    for step in job["steps"]:
                        if step["name"] == name:
                            step["state"] = st
                            if detail:
                                step["detail"] = self.p.mask(detail)
                            else:
                                step.pop("detail", None)

            with self.lock:
                job["state"] = "running"
            try:
                result = self.p.create(username, abs_user_id=abs_user_id, password=password, progress=progress)
                with self.lock:
                    job.update({"state": "done", "result": result, "finishedAt": utcnow()})
            except Exception as exc:  # noqa: BLE001
                msg = self.p.mask(exc if isinstance(exc, ProvisionError) else f"internal error: {type(exc).__name__}")
                LOG.error("job %s failed: %s", job_id, msg)
                with self.lock:
                    job.update({"state": "failed", "error": msg, "finishedAt": utcnow()})
            finally:
                password = None  # noqa: F841 - drop the only reference to the password
                self.queue.task_done()


def public_job(job: dict) -> dict:
    out = {k: job[k] for k in ("jobId", "state", "steps") if k in job}
    for k in ("result", "error", "slug", "username"):
        if job.get(k) is not None:
            out[k] = job[k]
    return out


class ApiHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"chaptarr-provision/{VERSION}"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    MAX_BODY = 64 * 1024

    # set by make_server
    provisioner: Provisioner
    jobs: JobManager
    token: bytes

    def log_message(self, fmt: str, *args: Any) -> None:  # route access log through masking logger
        LOG.info("%s %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: Any) -> None:
        data = (json.dumps(body, sort_keys=True) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization", "")
        expected = b"Bearer " + self.token
        return hmac.compare_digest(header.encode("utf-8", "replace"), expected)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length > self.MAX_BODY:
            raise ValidationError("request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise ValidationError("request body must be JSON") from None

    def _dispatch(self, method: str) -> None:
        try:
            if not self._authorized():
                # Drain the body so keep-alive clients do not desync.
                with contextlib.suppress(Exception):
                    n = int(self.headers.get("Content-Length") or 0)
                    if 0 < n <= self.MAX_BODY:
                        self.rfile.read(n)
                self._send(401, {"error": "unauthorized"})
                return
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in parsed.path.strip("/").split("/") if p]
            query = urllib.parse.parse_qs(parsed.query)
            route = (method, tuple(parts[:2]), len(parts))
            if route == ("GET", ("v1", "health"), 2):
                self._send(200, {"ok": True, "version": VERSION})
            elif route == ("GET", ("v1", "users"), 2):
                rows = self.provisioner.list_users()
                last = self.jobs.last_by_slug()
                for row in rows:
                    if row.get("slug") in last:
                        row["lastJobId"] = last[row["slug"]]
                self._send(200, rows)
            elif route == ("POST", ("v1", "users"), 2):
                self._create(self._body())
            elif route == ("GET", ("v1", "jobs"), 3):
                job = self.jobs.get(parts[2])
                if job:
                    self._send(200, public_job(job))
                else:
                    self._send(404, {"error": "job not found"})
            elif route == ("GET", ("v1", "users"), 3):
                self._send(200, self.provisioner.status(parts[2]))
            elif route == ("DELETE", ("v1", "users"), 3):
                slug = validate_slug(parts[2])
                if self.jobs.active_for_slug(slug):
                    self._send(409, {"error": "a create job is running for this user"})
                    return
                purge = (query.get("purge") or ["false"])[0].lower() in ("1", "true", "yes")
                self._send(200, self.provisioner.delete(slug, purge=purge))
            else:
                self._send(404, {"error": "not found"})
        except ValidationError as exc:
            msg = self.provisioner.mask(exc)
            self._send(404 if msg.startswith("no state for slug") else 400, {"error": msg})
        except BusyError as exc:
            self._send(409, {"error": self.provisioner.mask(exc)})
        except ProvisionError as exc:
            self._send(502, {"error": self.provisioner.mask(exc)})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("unhandled error: %s", type(exc).__name__)
            self._send(500, {"error": "internal error"})

    def _create(self, body: Any) -> None:
        if not isinstance(body, dict):
            raise ValidationError("body must be a JSON object")
        username = body.get("username")
        abs_user_id = body.get("absUserId") or None
        password = body.get("password") or None
        if not isinstance(username, str):
            raise ValidationError("username is required")
        for key, value in (("absUserId", abs_user_id), ("password", password)):
            if value is not None and not isinstance(value, str):
                raise ValidationError(f"{key} must be a string")
        slug = slugify(username)
        if password:
            self.provisioner.masker.add(password)
        if not abs_user_id and not password:
            state = self.provisioner.store.load(slug) or {}
            if not state.get("absUserId"):
                raise ValidationError("password is required when absUserId is not given (the ABS user is created)")
        job, _ = self.jobs.submit(username, abs_user_id, password)
        self._send(202, {"jobId": job["jobId"]})

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")


def parse_listen(listen: str) -> tuple[str, int]:
    host, _, port = listen.rpartition(":")
    if not host or not port.isdigit():
        raise ConfigError(f"invalid listen address {listen!r}; expected host:port")
    return host.strip("[]"), int(port)


def make_server(provisioner: Provisioner, listen: str, token: str | None) -> http.server.ThreadingHTTPServer:
    if not token or len(token) < 16:
        raise ConfigError("refusing to serve: server.token (or token_file / CHAPTARR_PROVISION_TOKEN) "
                          "must be configured with at least 16 characters")
    provisioner.masker.add(token)
    jobs = JobManager(provisioner)
    handler = type("BoundApiHandler", (ApiHandler,), {
        "provisioner": provisioner, "jobs": jobs, "token": token.encode()})
    host, port = parse_listen(listen)
    server = http.server.ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.jobs = jobs  # type: ignore[attr-defined]
    return server


# --------------------------------------------------------------------------- discover


REDACTED = "<redacted>"
DEFAULT_CONFIG_DIR = "/etc/chaptarr-provision"
PROVISIONER_NAME = "chaptarr-provisioner"
PROVISIONER_PORT = 8790
DEFAULT_PROVISIONER_IMAGE = "ghcr.io/pace-1/chaptarr-provisioner:develop"
# Provider field names that hold secrets, used when the template API cannot tell us which
# fields it masks. Category fields are never secrets.
SECRET_FIELD_RE = re.compile(r"(?i)(pass(word)?|api_?key|token|secret|cookie|mam_?id|session|seed)$")


class DiscoveryError(ProvisionError):
    pass


def toml_dumps(data: dict) -> str:
    """Serialize the config shapes chaptarr-provision uses (tables, arrays of tables, scalars)."""

    def key(k: str) -> str:
        return k if re.match(r"^[A-Za-z0-9_-]+$", k) else json.dumps(k)

    def scalar(v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, list):
            return "[" + ", ".join(scalar(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{ " + ", ".join(f"{key(k)} = {scalar(x)}" for k, x in v.items()) + " }"
        return json.dumps(str(v))

    def is_table_array(v: Any) -> bool:
        return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)

    lines: list[str] = []

    def body(path: list[str], table: dict) -> None:
        for k, v in table.items():
            if v is None or isinstance(v, dict) or is_table_array(v):
                continue
            lines.append(f"{key(k)} = {scalar(v)}")
        for k, v in table.items():
            if isinstance(v, dict):
                lines.append("")
                lines.append("[" + ".".join(key(p) for p in path + [k]) + "]")
                body(path + [k], v)
            elif is_table_array(v):
                for item in v:
                    lines.append("")
                    lines.append("[[" + ".".join(key(p) for p in path + [k]) + "]]")
                    body(path + [k], item)

    body([], data)
    return "\n".join(lines).strip() + "\n"


def redact(value: Any, secret_values: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: redact(v, secret_values) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret_values) for v in value]
    if isinstance(value, str) and value in secret_values:
        return REDACTED
    return value


def read_xml_tag(path: Path, tag: str) -> str | None:
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    m = re.search(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", text)
    return m.group(1) if m and m.group(1) else None


def sqlite_rows(db: Path, query: str) -> list[dict]:
    import sqlite3

    uri = "file:" + urllib.parse.quote(str(db)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(query).fetchall()]
    finally:
        conn.close()


class Container:
    """Read-only view over one `docker inspect` object."""

    def __init__(self, raw: dict):
        self.raw = raw

    @property
    def name(self) -> str:
        return str(self.raw.get("Name", "")).lstrip("/")

    @property
    def id(self) -> str:
        return str(self.raw.get("Id", ""))

    @property
    def image(self) -> str:
        return str((self.raw.get("Config") or {}).get("Image", ""))

    @property
    def env(self) -> dict:
        out = {}
        for item in (self.raw.get("Config") or {}).get("Env") or []:
            k, _, v = str(item).partition("=")
            out[k] = v
        return out

    @property
    def labels(self) -> dict:
        return (self.raw.get("Config") or {}).get("Labels") or {}

    @property
    def networks(self) -> dict:
        return (self.raw.get("NetworkSettings") or {}).get("Networks") or {}

    @property
    def mounts(self) -> list[dict]:
        return list(self.raw.get("Mounts") or [])

    def dns_names(self) -> set[str]:
        names = {self.name.lower(), self.id[:12].lower()}
        hostname = (self.raw.get("Config") or {}).get("Hostname")
        if hostname:
            names.add(str(hostname).lower())
        for net in self.networks.values():
            for alias in (net.get("Aliases") or []) + (net.get("DNSNames") or []):
                names.add(str(alias).lower())
        names.discard("")
        return names

    def ip(self, network: str | None = None) -> str | None:
        nets = self.networks
        if network and network in nets and nets[network].get("IPAddress"):
            return nets[network]["IPAddress"]
        for net in nets.values():
            if net.get("IPAddress"):
                return net["IPAddress"]
        return None

    def mount_at(self, container_path: str) -> tuple[dict, str] | None:
        """The mount whose destination is the longest prefix of ``container_path``, plus the rest."""
        best = None
        cp_ = norm_path(container_path)
        for m in self.mounts:
            dest = norm_path(str(m.get("Destination", "")))
            if cp_ == dest or cp_.startswith(dest.rstrip("/") + "/"):
                if best is None or len(dest) > len(norm_path(best["Destination"])):
                    best = m
        if best is None:
            return None
        rest = cp_[len(norm_path(best["Destination"])):].lstrip("/")
        return best, rest

    def host_path(self, container_path: str) -> str | None:
        found = self.mount_at(container_path)
        if not found or found[0].get("Type", "bind") != "bind":
            return None
        mount, rest = found
        return norm_path(str(mount["Source"]).rstrip("/") + ("/" + rest if rest else ""))

    def container_path_for_host(self, host_path: str) -> str | None:
        """Map a host path into this container through its bind mounts (longest source wins)."""
        best = None
        hp = norm_path(host_path)
        for m in self.mounts:
            if m.get("Type", "bind") != "bind":
                continue
            src = norm_path(str(m.get("Source", "")))
            if hp == src or hp.startswith(src.rstrip("/") + "/"):
                if best is None or len(src) > len(norm_path(best["Source"])):
                    best = m
        if best is None:
            return None
        rest = hp[len(norm_path(best["Source"])):].lstrip("/")
        return norm_path(str(best["Destination"]).rstrip("/") + ("/" + rest if rest else ""))


def list_containers(docker: DockerRunner) -> list[Container]:
    ids = docker.run(["ps", "-q", "--no-trunc"]).stdout.split()
    if not ids:
        return []
    raw = json.loads(docker.run(["inspect", *ids]).stdout or "[]")
    return [Container(c) for c in raw if (c.get("State") or {}).get("Running", True)]


def templatize(path: str, tokens: list[str]) -> str | None:
    """Replace the template owner's slug/username inside ``path`` with ``{slug}``.

    Returns None when no token occurs (the path is not per-user).
    """
    out = []
    hit = False
    for part in path.split("/"):
        new = part
        for tok in tokens:
            if len(tok) >= 2:
                pattern = re.compile(re.escape(tok), re.IGNORECASE)
                if pattern.search(new):
                    new = pattern.sub("{slug}", new)
                    hit = True
                    break
        out.append(new)
    return "/".join(out) if hit else None


def sibling_template(host_path: str) -> str:
    """For a shared (not per-user) folder, place each user's copy next to it: <dir>/<name>-{slug}."""
    parent, _, base = norm_path(host_path).rpartition("/")
    return f"{parent}/{base}-{{slug}}" if base else "/{slug}"


def static_prefix(template: str) -> str:
    """The deepest directory of a path template that contains no placeholder."""
    if "{" not in template:
        return norm_path(template)
    return norm_path(template.split("{", 1)[0].rpartition("/")[0] or "/")


class Discovery:
    """Inspects the Docker host and builds a complete config for chaptarr-provision."""

    def __init__(self, docker: DockerRunner, http: HttpClient | None = None, abs_container: str | None = None,
                 template: str | None = None, config_dir: str = DEFAULT_CONFIG_DIR):
        self.docker = docker
        self.http = http or HttpClient(10)
        self.abs_container_arg = abs_container
        self.template_arg = template
        self.config_dir = config_dir.rstrip("/")
        self.notes: list[str] = []
        self.warnings: list[str] = []
        self.secret_values: set[str] = set()
        self.profiles_text: str | None = None
        self._abs_mount_warned: set[str] = set()

    def note(self, text: str) -> None:
        self.notes.append(text)

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    def secret(self, value: Any) -> Any:
        if isinstance(value, str) and value:
            self.secret_values.add(value)
        return value

    # ---- containers

    def find_abs(self, containers: list[Container]) -> Container:
        if self.abs_container_arg:
            wanted = self.abs_container_arg.lower()
            for c in containers:
                if c.name.lower() == wanted or c.id.startswith(wanted):
                    return c
            raise DiscoveryError(f"no running container named {self.abs_container_arg!r}")
        found = [c for c in containers if "audiobookshelf" in c.image.lower() or "audiobookshelf" in c.name.lower()]
        if not found:
            names = ", ".join(sorted(c.name for c in containers)) or "none"
            raise DiscoveryError(f"no running Audiobookshelf container found (running: {names}); pass --abs-container")
        if len(found) > 1:
            names = ", ".join(f"{c.name} ({c.image})" for c in found)
            raise DiscoveryError(f"several Audiobookshelf containers match: {names}; pass --abs-container NAME")
        return found[0]

    def by_hostname(self, containers: list[Container], url: str) -> Container | None:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        if not host:
            return None
        for c in containers:
            if host in c.dns_names():
                return c
        for c in containers:  # an IP address in the profile
            if host in {str(n.get("IPAddress", "")) for n in c.networks.values()}:
                return c
        return None

    # ---- main

    def run(self) -> dict:
        containers = list_containers(self.docker)
        abs_c = self.find_abs(containers)
        self.note(f"Audiobookshelf container: {abs_c.name} ({abs_c.image})")

        nets = list(abs_c.networks)
        if not nets:
            raise DiscoveryError(f"{abs_c.name} is not on any Docker network")
        network = nets[0]
        if len(nets) > 1:
            self.note(f"ABS is on several networks ({', '.join(nets)}); using the first: {network}")
        if network in ("bridge", "host", "none"):
            self.warn(f"ABS uses the {network!r} network, which has no container DNS. Put ABS on a user-defined "
                      "network so it can reach chaptarr-<slug> by name.")

        abs_config = abs_c.host_path("/config")
        if not abs_config:
            raise DiscoveryError(f"{abs_c.name} has no bind mount at /config, so its plugin settings cannot be read")
        self.note(f"ABS config folder on the host: {abs_config}")

        settings = self.read_abs_settings(Path(abs_config))
        profiles = settings["profiles"]
        abs_port = abs_c.env.get("PORT") or "80"
        abs_url = f"http://{abs_c.name}:{abs_port}"

        template_c, owner_id = self.choose_template(containers, settings)
        template_url = f"http://{template_c.name}:{CHAPTARR_PORT}"
        api_key = self.template_api_key(template_c)

        owner_profile = profiles.get(owner_id) if owner_id else None
        tokens = self.owner_tokens(template_c, owner_id, owner_profile, Path(abs_config))
        roots = self.template_roots(template_c, owner_profile)

        env = template_c.env
        docker_cfg: dict[str, Any] = {
            "network": network,
            "image": template_c.image or DEFAULT_IMAGE,
            "puid": int(env["PUID"]) if env.get("PUID", "").isdigit() else 1000,
            "pgid": int(env["PGID"]) if env.get("PGID", "").isdigit() else 1000,
            "tz": env.get("TZ") or "Etc/UTC",
        }
        if env.get("UMASK"):
            docker_cfg["umask"] = env["UMASK"]
        for k in ("PUID", "PGID", "TZ"):
            if not env.get(k):
                self.note(f"template has no {k}; using {docker_cfg[k.lower()]}")

        paths, extra_volumes, downloads_dest = self.infer_paths(template_c, abs_c, tokens, roots)
        if extra_volumes:
            docker_cfg["extra_volumes"] = extra_volumes

        raw: dict[str, Any] = {
            "state_dir": "/var/lib/chaptarr-provision",
            "stacks_dir": "/opt/chaptarr-stacks",
            "hooks_dir": f"{self.config_dir}/hooks.d",
            "server": {"listen": DEFAULT_LISTEN, "token_file": f"{self.config_dir}/token"},
            "abs": {"url": abs_url, "api_key_file": f"{self.config_dir}/abs_api_key", "container": abs_c.name,
                    "config_dir": abs_config},
            "docker": docker_cfg,
            "paths": paths,
            "template": {"url": template_url, "api_key": self.secret(api_key)},
        }

        clients, indexer_secrets = self.read_providers(template_c, api_key, tokens)
        if clients:
            raw["download_clients"] = clients
        if indexer_secrets:
            raw["indexer_secrets"] = indexer_secrets

        prowlarr = self.find_prowlarr(containers, network, template_c)
        if prowlarr:
            raw["prowlarr"] = prowlarr

        if not settings.get("url"):
            self.note("ABS plugin has no shared Chaptarr url set")
        return {"config": raw, "settings": settings, "downloads_dest": downloads_dest}

    # ---- ABS settings

    def read_abs_settings(self, abs_config: Path) -> dict:
        path = abs_config / "plugins" / ".state" / "chaptarr" / "settings.json"
        if not path.exists():
            legacy = abs_config / "plugins" / "chaptarr" / "settings.json"
            path = legacy if legacy.exists() else path
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            raise DiscoveryError(f"Chaptarr plugin settings not found at {path}; install and configure the "
                                 "Chaptarr plugin in ABS first") from None
        except ValueError as exc:
            raise DiscoveryError(f"{path} is not JSON: {exc}") from None
        self.note(f"plugin settings: {path}")
        raw_profiles = data.get("userProfiles") or ""
        profiles = parse_profiles_input(json.dumps(raw_profiles)) if raw_profiles else {}
        for prof in profiles.values():
            self.secret(prof.get("apiKey"))
        self.secret(data.get("apiKey"))
        self.secret(data.get("password"))
        self.secret(data.get("provisionerToken"))
        self.profiles_text = json.dumps(profiles) if profiles else None
        url = str(data.get("url") or "").strip()
        self.note(f"{len(profiles)} existing user profile(s): {', '.join(sorted(profiles)) or 'none'}")
        self.note(f"shared admin connection url: {url or 'not set'}")
        self.note(f"provisionerUrl is {'set to ' + data['provisionerUrl'] if data.get('provisionerUrl') else 'not set'}")
        return {"profiles": profiles, "url": url, "provisionerUrl": data.get("provisionerUrl") or "",
                "path": str(path), "raw": data}

    # ---- template

    def choose_template(self, containers: list[Container], settings: dict) -> tuple[Container, str | None]:
        profiles = settings["profiles"]
        mapped: dict[str, Container] = {}
        for uid, prof in profiles.items():
            c = self.by_hostname(containers, prof.get("url", ""))
            if c:
                mapped[uid] = c
                self.note(f"profile {uid}: {prof.get('url')} -> container {c.name}")
            else:
                self.warn(f"profile {uid}: {prof.get('url')} does not match a running container")

        def owner_of(c: Container) -> str | None:
            return next((uid for uid, mc in mapped.items() if mc.id == c.id), None)

        if self.template_arg:
            wanted = self.template_arg.lower()
            for c in containers:
                if c.name.lower() in (wanted, f"chaptarr-{wanted}") or wanted in c.dns_names():
                    self.note(f"template (from --template): {c.name}")
                    return c, owner_of(c)
            raise DiscoveryError(f"--template {self.template_arg!r} does not match a running container")
        if settings.get("url"):
            c = self.by_hostname(containers, settings["url"])
            if c:
                self.note(f"template: {c.name} (the plugin's shared admin connection)")
                return c, owner_of(c)
            self.warn(f"shared url {settings['url']} does not match a running container")
        if mapped:
            uid, c = next(iter(mapped.items()))
            self.note(f"template: {c.name} (first profile, {uid})")
            return c, uid
        raise DiscoveryError("cannot find a template Chaptarr: no shared url or profile maps to a running container; "
                             "pass --template CONTAINER")

    def template_api_key(self, c: Container) -> str:
        key = c.env.get("Chaptarr__Auth__ApiKey")
        if key:
            self.note("template API key: from its Chaptarr__Auth__ApiKey env")
            return key
        cfg = c.host_path("/config")
        key = read_xml_tag(Path(cfg) / "config.xml", "ApiKey") if cfg else None
        if not key:
            raise DiscoveryError(f"cannot read the API key of {c.name}: no Chaptarr__Auth__ApiKey env and no "
                                 f"<ApiKey> in {cfg or '(no /config mount)'}/config.xml")
        self.note(f"template API key: from {cfg}/config.xml")
        return key

    def owner_tokens(self, c: Container, owner_id: str | None, profile: dict | None, abs_config: Path) -> list[str]:
        tokens: list[str] = []
        username = None
        if owner_id:
            db = abs_config / "absdatabase.sqlite"
            with contextlib.suppress(Exception):
                rows = sqlite_rows(db, "SELECT id, username FROM users")
                username = next((r["username"] for r in rows if r["id"] == owner_id), None)
        if not username and profile:
            for media in ("ebook", "audiobook"):
                label = str((profile.get(media) or {}).get("label") or "")
                m = re.match(r"^(.+?)\s+(e-?books?|audiobooks?)$", label, re.IGNORECASE)
                if m:
                    username = m.group(1)
                    break
        if username:
            tokens += [username, username.lower()]
            with contextlib.suppress(ValidationError):
                tokens.append(slugify(username))
        prefix = "chaptarr-"
        if c.name.lower().startswith(prefix) and len(c.name) > len(prefix):
            tokens.append(c.name[len(prefix):].lower())
        tokens = sorted(set(tokens), key=len, reverse=True)
        self.note(f"template owner: {username or 'unknown'} (path tokens: {', '.join(tokens) or 'none'})")
        return tokens

    def template_roots(self, c: Container, profile: dict | None) -> dict:
        roots = {}
        if profile:
            for media in ("ebook", "audiobook"):
                if (profile.get(media) or {}).get("path"):
                    roots[media] = profile[media]["path"]
        if len(roots) < 2:
            db = self.template_db(c)
            if db:
                with contextlib.suppress(Exception):
                    for r in sqlite_rows(db, 'SELECT "Path", "FolderType" FROM "RootFolders"'):
                        media = {FOLDER_TYPE_EBOOK: "ebook", FOLDER_TYPE_AUDIOBOOK: "audiobook"}.get(r["FolderType"])
                        if media and media not in roots:
                            roots[media] = r["Path"]
        for media in ("ebook", "audiobook"):
            if media not in roots:
                guess = "/books/ebooks" if media == "ebook" else "/books/audiobooks"
                roots[media] = guess
                self.warn(f"no {media} root folder found on the template; assumed {guess}")
        self.note(f"template root folders: ebook {roots['ebook']}, audiobook {roots['audiobook']}")
        return roots

    def template_db(self, c: Container) -> Path | None:
        cfg = c.host_path("/config")
        if not cfg:
            return None
        for name in ("chaptarr.db", "readarr.db", "audioarr.db"):  # PathExtensions.GetDatabase order
            p = Path(cfg) / name
            if p.exists():
                return p
        return None

    # ---- paths

    def infer_paths(self, t: Container, abs_c: Container, tokens: list[str], roots: dict) -> tuple[dict, list, str]:
        paths: dict[str, dict] = {}
        used: set[str] = set()

        def per_user(host: str, what: str) -> str:
            tmpl = templatize(host, tokens)
            if tmpl:
                return tmpl
            tmpl = sibling_template(host)
            self.note(f"{what}: {host} is not per-user; new users get {tmpl}")
            return tmpl

        cfg_mount = t.mount_at("/config")
        if not cfg_mount or cfg_mount[0].get("Type", "bind") != "bind":
            raise DiscoveryError(f"template {t.name} has no bind mount at /config")
        used.add(norm_path(cfg_mount[0]["Destination"]))
        paths["config"] = {"host": per_user(t.host_path("/config"), "config"), "container": "/config"}

        for media, key in (("ebook", "ebooks"), ("audiobook", "audiobooks")):
            container_path = roots[media]
            found = t.mount_at(container_path)
            if not found:
                raise DiscoveryError(f"template {t.name}: no mount contains its {media} root {container_path}")
            used.add(norm_path(found[0]["Destination"]))
            host = t.host_path(container_path)
            entry = {"host": per_user(host, key),
                     "container": templatize(container_path, tokens) or container_path}
            entry["abs"] = self.abs_side(abs_c, entry["host"], key)
            paths[key] = entry

        downloads_dest = ""
        extra: list[str] = []
        dl = next((m for m in t.mounts if "download" in str(m.get("Destination", "")).lower()
                   and m.get("Type", "bind") == "bind" and norm_path(str(m["Destination"])) not in used), None)
        if dl:
            dest, src = norm_path(dl["Destination"]), norm_path(dl["Source"])
            used.add(dest)
            downloads_dest = dest
            per = templatize(src, tokens)
            if per:
                paths["downloads"] = {"host": per, "container": templatize(dest, tokens) or dest}
            else:
                extra.append(f"{src}:{dest}" + ("" if dl.get("RW", True) else ":ro"))
                paths["downloads"] = {"host": f"{src}/chaptarr-{{slug}}", "container": f"{dest}/chaptarr-{{slug}}"}
                self.note(f"downloads: {src} is shared; mounted as-is, per-user folders go in chaptarr-{{slug}}/")
        else:
            cfg_host = paths["config"]["host"]
            parent = cfg_host.rsplit("/", 1)[0]
            paths["downloads"] = {"host": f"{parent}/downloads", "container": "/downloads"}
            self.warn(f"template has no downloads mount; assumed {parent}/downloads per user")

        for m in t.mounts:
            dest = norm_path(str(m.get("Destination", "")))
            if dest in used or m.get("Type", "bind") != "bind":
                if m.get("Type") not in (None, "bind") and dest not in used:
                    self.note(f"skipped {m.get('Type')} mount at {dest}")
                continue
            src = norm_path(str(m.get("Source", "")))
            if dest == "/var/run/docker.sock":
                continue
            src_t = templatize(src, tokens) or src
            extra.append(f"{src_t}:{dest}" + ("" if m.get("RW", True) else ":ro"))
            self.note(f"extra volume: {src_t}:{dest}")
        return paths, extra, downloads_dest

    def abs_side(self, abs_c: Container, host_template: str, key: str) -> str:
        probe = host_template.replace("{slug}", "zz-probe-slug")
        mapped = abs_c.container_path_for_host(probe)
        if mapped:
            return mapped.replace("zz-probe-slug", "{slug}")
        parent = static_prefix(host_template)
        if parent in self._abs_mount_warned:
            self.note(f"{key}: also needs the ABS mount {parent}:{parent} (see warning)")
            return host_template
        self._abs_mount_warned.add(parent)
        self.warn(f"{key}: new users' folders ({host_template}) are outside every ABS mount. Add a parent mount to "
                  f"the ABS service and recreate it:\n      volumes:\n        - {parent}:{parent}\n"
                  f"    The config assumes that mount (abs path {host_template}).")
        return host_template

    # ---- providers

    def masked_fields(self, c: Container, api_key: str, kind: str) -> dict[str, set[str]] | None:
        ip = c.ip()
        if not ip:
            return None
        try:
            items = chaptarr_client(self.http, f"http://{ip}:{CHAPTARR_PORT}", api_key).get(f"/api/v1/{kind}") or []
        except ProvisionError:
            return None
        return {i.get("name", ""): {f["name"] for f in i.get("fields") or [] if f.get("value") == SECRET_PLACEHOLDER}
                for i in items}

    def read_providers(self, t: Container, api_key: str, tokens: list[str]) -> tuple[list, dict]:
        if t.env.get("Chaptarr__Postgres__Host"):
            return self.providers_from_api_only(t, api_key)
        db = self.template_db(t)
        if not db:
            self.warn(f"template database not found under {t.host_path('/config')}; download clients and indexer "
                      "secrets must be added by hand")
            return [], {}
        self.note(f"template database: {db} (read-only)")
        clients_masked = self.masked_fields(t, api_key, "downloadclient")
        indexers_masked = self.masked_fields(t, api_key, "indexer")
        if clients_masked is None:
            self.note("template API not reachable from here; secret fields picked by name")
        rows = sqlite_rows(db, 'SELECT "Id", "Name", "Implementation", "Settings" FROM "DownloadClients"')
        rpms = sqlite_rows(db, 'SELECT "DownloadClientId", "Host", "RemotePath", "LocalPath" FROM "RemotePathMappings"')
        clients = []
        for r in rows:
            settings = json.loads(r["Settings"] or "{}")
            secret_names = self._secret_names(settings, (clients_masked or {}).get(r["Name"]))
            entry: dict[str, Any] = {"name": r["Name"], "implementation": r["Implementation"], "from_template": True}
            fields = {k: self.secret(settings[k]) for k in sorted(secret_names) if settings.get(k)}
            if fields:
                entry["fields"] = fields
            maps = [{"remote_path": m["RemotePath"],
                     "local_path": templatize(m["LocalPath"], tokens) or m["LocalPath"],
                     **({"host": m["Host"]} if m.get("Host") else {})}
                    for m in rpms if m["DownloadClientId"] == r["Id"]]
            if maps:
                entry["remote_path_mappings"] = maps
            clients.append(entry)
            self.note(f"download client {r['Name']} ({r['Implementation']}): secrets {', '.join(sorted(fields)) or 'none'}, "
                      f"{len(maps)} remote path mapping(s)")
        indexer_secrets = {}
        for r in sqlite_rows(db, 'SELECT "Name", "Implementation", "Settings" FROM "Indexers"'):
            settings = json.loads(r["Settings"] or "{}")
            names = self._secret_names(settings, (indexers_masked or {}).get(r["Name"]))
            vals = {k: self.secret(settings[k]) for k in sorted(names) if settings.get(k)}
            if vals:
                indexer_secrets[r["Name"]] = vals
            self.note(f"indexer {r['Name']}: secrets {', '.join(sorted(vals)) or 'none'}")
        return clients, indexer_secrets

    @staticmethod
    def _secret_names(settings: dict, masked: set[str] | None) -> set[str]:
        names = set(masked) if masked is not None else {k for k in settings if SECRET_FIELD_RE.search(k)}
        return {n for n in names if "category" not in n.lower() and "." not in n}

    def providers_from_api_only(self, t: Container, api_key: str) -> tuple[list, dict]:
        self.warn(f"template {t.name} uses PostgreSQL: its database was not read. Fill in every CHANGE-ME value "
                  "in the config by hand.")
        clients, idx = [], {}
        for name, fields in (self.masked_fields(t, api_key, "downloadclient") or {}).items():
            entry: dict[str, Any] = {"name": name, "from_template": True}
            if fields:
                entry["fields"] = {f: "CHANGE-ME" for f in sorted(fields) if "category" not in f.lower()}
                self.warn(f"download client {name}: missing secret(s) {', '.join(sorted(entry['fields']))}")
            clients.append(entry)
        for name, fields in (self.masked_fields(t, api_key, "indexer") or {}).items():
            if fields:
                idx[name] = {f: "CHANGE-ME" for f in sorted(fields)}
                self.warn(f"indexer {name}: missing secret(s) {', '.join(sorted(fields))}")
        return clients, idx

    # ---- prowlarr

    def find_prowlarr(self, containers: list[Container], network: str, t: Container) -> dict | None:
        found = [c for c in containers if "prowlarr" in c.image.lower()]
        if not found:
            self.note("Prowlarr: not found; indexers will be copied from the template")
            return None
        p = next((c for c in found if network in c.networks), None)
        if not p:
            self.note(f"Prowlarr: {found[0].name} is not on {network}; indexers will be copied from the template")
            return None
        cfg = p.host_path("/config")
        key = read_xml_tag(Path(cfg) / "config.xml", "ApiKey") if cfg else None
        port = read_xml_tag(Path(cfg) / "config.xml", "Port") if cfg else None
        port = port if port and port.isdigit() else "9696"
        if not key:
            self.note(f"Prowlarr: {p.name} found but its API key is unreadable; indexers will be copied")
            return None
        apps = None
        ip = p.ip(network)
        if ip:
            with contextlib.suppress(ProvisionError):
                apps = chaptarr_client(self.http, f"http://{ip}:{port}", key).get("/api/v1/applications")
        if apps is None:
            db = Path(cfg) / "prowlarr.db"
            with contextlib.suppress(Exception):
                apps = [{"implementation": r["Implementation"], "name": r["Name"],
                         "fields": [{"name": k, "value": v} for k, v in json.loads(r["Settings"] or "{}").items()]}
                        for r in sqlite_rows(db, 'SELECT "Name", "Implementation", "Settings" FROM "Applications"')]
        names = t.dns_names() | {n.get("IPAddress", "") for n in t.networks.values()}
        app = next((a for a in apps or [] if (urllib.parse.urlparse(str(fields_to_dict(a).get("baseUrl", ""))).hostname
                                             or "").lower() in names), None)
        if not app:
            self.note(f"Prowlarr: {p.name} has no application pointing at {t.name}; indexers will be copied")
            return None
        self.note(f"Prowlarr: {p.name} syncs {t.name} as {app.get('implementation')}; new instances are added there")
        url = f"http://{p.name}:{port}"
        return {"url": url, "api_key": self.secret(key), "implementation": app.get("implementation") or "Readarr",
                "prowlarr_url": str(fields_to_dict(app).get("prowlarrUrl") or url)}


def discover_main(args: argparse.Namespace, docker: DockerRunner, http: HttpClient | None, masker: Masker) -> int:
    d = Discovery(docker, http, abs_container=args.abs_container, template=args.template,
                  config_dir=str(Path(args.write).parent) if args.write else DEFAULT_CONFIG_DIR)
    out = d.run()
    masker.add(*d.secret_values)
    raw = out["config"]
    raw["state_dir"] = args.state_dir
    header = ("# Generated by `chaptarr-provision discover` on " + utcnow() + ".\n"
              "# Review the summary it printed; every assumption is listed there.\n\n")
    if args.write:
        target = Path(args.write)
        atomic_write(target, header + toml_dumps(raw), mode=0o600)
        d.note(f"wrote {target} (mode 0600)")
    else:
        print(header + toml_dumps(redact(raw, d.secret_values)), end="")
    if args.import_profiles:
        if d.profiles_text:
            store = StateStore(Path(raw["state_dir"]))
            res = import_profiles_into(store, d.profiles_text)
            d.note(f"imported {res['imported']} profile(s) into {store.profiles_path} "
                   f"(added {len(res['added'])}, updated {len(res['updated'])})")
        else:
            d.note("no profiles to import")
    lines = ["", "Discovery summary:"] + [f"  - {n}" for n in d.notes]
    if d.warnings:
        lines += ["", "WARNINGS:"] + [f"  ! {w}" for w in d.warnings]
    lines += ["", "Not discoverable: the ABS admin API key (pass it to install-service --abs-api-key-file)."]
    print(masker.mask("\n".join(lines)), file=sys.stderr if not args.write else sys.stdout)
    return 0


def import_profiles_into(store: StateStore, text: str) -> dict:
    incoming = parse_profiles_input(text)
    with store.lock("_profiles"):
        merged = store.load_profiles()
        added = [k for k in incoming if k not in merged]
        updated = [k for k in incoming if k in merged and merged[k] != incoming[k]]
        merged.update(incoming)
        store.save_profiles(merged)
    return {"imported": len(incoming), "added": sorted(added), "updated": sorted(updated), "total": len(merged)}


# --------------------------------------------------------------------------- install-service


def service_mounts(cfg: Config, config_path: Path) -> list[str]:
    """Paths the provisioner container needs, mounted at identical paths (compose resolves on the host)."""
    roots = ["/var/run/docker.sock", str(config_path.parent), str(cfg.state_dir), str(cfg.stacks_dir)]
    for f in (cfg.raw.get("server") or {}).get("token_file"), (cfg.raw.get("abs") or {}).get("api_key_file"):
        if f:
            roots.append(str(Path(f).parent))
    if cfg.hooks_dir:
        roots.append(str(cfg.hooks_dir))
    for key in FOLDER_KEYS:
        host = (cfg.paths.get(key) or {}).get("host")
        if host:
            roots.append(static_prefix(host))
    for vol in cfg.extra_volumes:
        src = str(vol).split(":", 1)[0]
        roots.append(static_prefix(src) if "{" in src else src)
    out: list[str] = []
    for r in sorted({norm_path(r) for r in roots if r and r != "/"}, key=len):
        if not any(r == o or r.startswith(o.rstrip("/") + "/") for o in out):
            out.append(r)
    return out


def render_service_compose(cfg: Config, config_path: Path, image: str) -> str:
    vols = "\n".join(f"      - {json.dumps(f'{m}:{m}')}" for m in service_mounts(cfg, config_path))
    return f"""# Generated by `chaptarr-provision install-service`. Re-run it to update.
# Internal service: no published ports. Only the ABS container should reach it.
name: {PROVISIONER_NAME}

services:
  provisioner:
    image: {json.dumps(image)}
    container_name: {PROVISIONER_NAME}
    hostname: {PROVISIONER_NAME}
    restart: unless-stopped
    command: ["serve", "--listen", "0.0.0.0:{PROVISIONER_PORT}"]
    environment:
      CHAPTARR_PROVISION_CONFIG: {json.dumps(str(config_path))}
    volumes:
{vols}
    networks:
      - shared

networks:
  shared:
    name: {json.dumps(cfg.network)}
    external: true
"""


def plan_settings_patch(plugin: dict | None, desired: dict) -> tuple[bool, str]:
    """Decide whether PATCHing only ``desired`` is safe for this ABS plugin.

    ABS (PluginSdkManager.updateSettings -> validateSettings(manifest, values, previous, patch=true))
    starts from the previous settings, applies only the keys sent, keeps a secret when the patch
    sends '' for it, and rejects unknown keys. So a partial PATCH keeps userProfiles and apiKey,
    provided the schema knows both keys. Anything else is refused here.
    """
    if not plugin:
        return False, "the Chaptarr plugin is not installed in ABS"
    keys = {f.get("key") for f in plugin.get("settingsSchema") or []}
    missing = [k for k in desired if k not in keys]
    if missing:
        return False, f"this plugin version has no setting(s) {', '.join(missing)}; update the plugin first"
    if not isinstance(plugin.get("secretsSet"), dict):
        return False, "ABS did not report which secrets are set, so a partial update cannot be verified"
    return True, "ok"


def install_service_main(args: argparse.Namespace, docker: DockerRunner, http: HttpClient | None,
                         masker: Masker, sleep: Callable[[float], None] = time.sleep) -> int:
    config_path = Path(args.config)
    raw = tomllib.loads(config_path.read_text())
    server = raw.get("server") or {}
    abs_ = raw.get("abs") or {}
    report: list[str] = []

    token_file = Path(server.get("token_file") or f"{config_path.parent}/token")
    if server.get("token"):
        token = str(server["token"])
        report.append("serve token: inline in config")
    elif token_file.exists() and token_file.read_text().strip():
        token = token_file.read_text().strip()
        report.append(f"serve token: kept {token_file}")
    else:
        token = secrets.token_urlsafe(32)
        atomic_write(token_file, token + "\n", mode=0o600)
        report.append(f"serve token: generated {token_file} (mode 0600)")
    masker.add(token)

    abs_key = Path(args.abs_api_key_file).read_text().strip()
    if not abs_key:
        raise ValidationError(f"{args.abs_api_key_file} is empty")
    masker.add(abs_key)
    key_target = Path(abs_.get("api_key_file") or f"{config_path.parent}/abs_api_key")
    if not abs_.get("api_key"):
        current = key_target.read_text().strip() if key_target.exists() else None
        if current != abs_key:
            atomic_write(key_target, abs_key + "\n", mode=0o600)
            report.append(f"ABS API key: stored in {key_target} (mode 0600)")

    cfg = load_config(config_path)
    cfg.validate(need_server=True)
    stack = cfg.stacks_dir / PROVISIONER_NAME
    compose = stack / "compose.yaml"
    text = render_service_compose(cfg, config_path, args.image)
    changed = not compose.exists() or compose.read_text() != text
    atomic_write(compose, text, mode=0o644)
    report.append(f"compose: {compose} {'written' if changed else 'unchanged'}")
    docker.run(["compose", "-f", str(compose), "-p", PROVISIONER_NAME, "up", "-d", "--remove-orphans"],
               cwd=str(stack))

    client = http or HttpClient(10)
    health = None
    deadline = time.monotonic() + float(args.timeout)
    while True:
        ip = None
        with contextlib.suppress(Exception):
            info = json.loads(docker.run(["inspect", PROVISIONER_NAME]).stdout)[0]
            ip = Container(info).ip(cfg.network)
        if ip:
            with contextlib.suppress(ProvisionError):
                health = ApiClient(client, f"http://{ip}:{PROVISIONER_PORT}",
                                   {"Authorization": f"Bearer {token}"}).get("/v1/health")
        if health or time.monotonic() > deadline:
            break
        sleep(1)
    if not health:
        raise ProvisionError(f"{PROVISIONER_NAME} did not answer /v1/health within {args.timeout}s; "
                             f"see `docker logs {PROVISIONER_NAME}`")
    report.append(f"service healthy (version {health.get('version')})")

    desired = {"provisionerUrl": f"http://{PROVISIONER_NAME}:{PROVISIONER_PORT}", "provisionerToken": token}
    abs_api = abs_client(client, cfg.abs_url, abs_key)
    try:
        plugins = (abs_api.get("/api/plugins") or {}).get("plugins", [])
    except HttpError:
        raise
    except ProvisionError:
        # On the host, container names do not resolve: go to the ABS container's IP instead.
        name = abs_.get("container")
        info = json.loads(docker.run(["inspect", name]).stdout)[0] if name else None
        ip = Container(info).ip(cfg.network) if info else None
        if not ip:
            raise
        port = urllib.parse.urlparse(cfg.abs_url).port or 80
        abs_api = abs_client(client, f"http://{ip}:{port}", abs_key)
        plugins = (abs_api.get("/api/plugins") or {}).get("plugins", [])
    plugin = next((p for p in plugins if p.get("id") == cfg.abs_plugin_id), None)
    ok, why = plan_settings_patch(plugin, desired)
    if ok:
        # Always re-send: idempotent, and it repairs a token that drifted.
        before = {k for k, v in plugin["secretsSet"].items() if v}
        res = abs_api.patch(f"/api/plugins/{cfg.abs_plugin_id}/settings", desired) or {}
        updated = res.get("plugin") if isinstance(res, dict) else None
        if not isinstance(updated, dict):
            updated = next((p for p in (abs_api.get("/api/plugins") or {}).get("plugins", [])
                            if p.get("id") == cfg.abs_plugin_id), {})
        after = {k for k, v in (updated.get("secretsSet") or {}).items() if v}
        lost = sorted(before - after)
        if lost:
            raise ProvisionError(f"ABS dropped secret setting(s) {', '.join(lost)} after the update; re-enter "
                                 "them in Settings > Plugins > Chaptarr (userProfiles can be restored from "
                                 f"{cfg.state_dir}/profiles.json)")
        report.append(f"ABS plugin: provisionerUrl = {desired['provisionerUrl']}, provisionerToken set "
                      f"(kept: {', '.join(sorted(before - set(desired))) or 'nothing else was set'})")
    else:
        report.append(f"ABS plugin NOT updated: {why}. Set these in Settings > Plugins > Chaptarr:")
        report.append(f"    Provisioner address: {desired['provisionerUrl']}")
        report.append(f"    Provisioner token:   the contents of {token_file}")
    print(masker.mask("\n".join(report)))
    return 0 if ok else 1


# --------------------------------------------------------------------------- CLI


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def _print_table(rows: Iterable[dict], cols: list[str]) -> None:
    rows = list(rows)
    widths = {c: max([len(c)] + [len(str(r.get(c) or "")) for r in rows]) for c in cols}
    print("  ".join(c.upper().ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c) or "").ljust(widths[c]) for c in cols))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="chaptarr-provision", description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", "-c", default=os.environ.get("CHAPTARR_PROVISION_CONFIG", DEFAULT_CONFIG_PATH))
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create", help="provision (or resume provisioning) an instance for a user")
    p.add_argument("username")
    p.add_argument("--abs-user-id", help="existing ABS user id; omit to create the ABS user")
    p.add_argument("--password-stdin", action="store_true", help="read the new ABS user's password from stdin")

    p = sub.add_parser("list", help="list provisioned users")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("status", help="show state for one user (no secrets)")
    p.add_argument("slug")

    p = sub.add_parser("delete", help="remove a user's container and deregister the profile")
    p.add_argument("slug")
    p.add_argument("--purge", action="store_true", help="also delete the user's data folders and state")

    p = sub.add_parser("import-profiles", help="seed profiles.json from the plugin's userProfiles JSON")
    p.add_argument("file", help="path, or - for stdin")

    p = sub.add_parser("discover", help="inspect the Docker host and generate config.toml")
    p.add_argument("--abs-container", help="ABS container name (default: auto-detect)")
    p.add_argument("--template", help="template Chaptarr: slug or container name (default: auto)")
    p.add_argument("--write", metavar="PATH", help="write the config here (mode 0600); default: print it redacted")
    p.add_argument("--import-profiles", action="store_true", help="also seed profiles.json from the ABS plugin")
    p.add_argument("--state-dir", default="/var/lib/chaptarr-provision", help="state_dir to write into the config")

    p = sub.add_parser("install-service", help="run `serve` as a container on the ABS network and point ABS at it")
    p.add_argument("--abs-api-key-file", required=True, help="file holding an ABS admin API key")
    p.add_argument("--image", default=DEFAULT_PROVISIONER_IMAGE)
    p.add_argument("--timeout", type=float, default=90, help="seconds to wait for /v1/health")

    p = sub.add_parser("serve", help="run the HTTP API for the ABS plugin")
    p.add_argument("--listen", help=f"host:port (default from config, else {DEFAULT_LISTEN})")
    return ap


def main(argv: list[str] | None = None, *, http: HttpClient | None = None,
         docker: DockerRunner | None = None) -> int:
    args = build_parser().parse_args(argv)
    masker = Masker()
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(MaskingFilter(masker))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.handlers[:] = [handler]
    LOG.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    LOG.propagate = False
    try:
        if args.cmd == "discover":
            return discover_main(args, docker or DockerRunner(["docker"]), http, masker)
        if args.cmd == "install-service":
            return install_service_main(args, docker or DockerRunner(["docker"]), http, masker)
        cfg = load_config(args.config)
        cfg.validate(need_server=args.cmd == "serve")
        prov = Provisioner(cfg, http=http, docker=docker, masker=masker)
        if args.cmd == "create":
            password = None
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\r\n") or None
                if not password:
                    raise ValidationError("--password-stdin given but no password was read")

            def progress(name: str, state: str, detail: str | None) -> None:
                if state != "running":
                    print(f"[{state:>6}] {name}" + (f": {detail}" if detail else ""), file=sys.stderr)

            _print_json(prov.create(args.username, abs_user_id=args.abs_user_id, password=password,
                                    progress=progress))
        elif args.cmd == "list":
            rows = prov.list_users()
            if args.json:
                _print_json(rows)
            else:
                _print_table(rows, ["slug", "username", "absUserId", "state", "url"])
        elif args.cmd == "status":
            _print_json(prov.status(args.slug))
        elif args.cmd == "delete":
            out = prov.delete(args.slug, purge=args.purge)
            _print_json(out)
            return 1 if out.get("error") else 0
        elif args.cmd == "import-profiles":
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text()
            _print_json(prov.import_profiles(text))
        elif args.cmd == "serve":
            server = make_server(prov, args.listen or cfg.listen, cfg.token)
            LOG.info("chaptarr-provision %s listening on %s", VERSION, args.listen or cfg.listen)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        return 0
    except ValidationError as exc:
        print(f"error: {masker.mask(exc)}", file=sys.stderr)
        return 2
    except (ProvisionError, OSError) as exc:
        print(f"error: {masker.mask(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
