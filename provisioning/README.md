# chaptarr-provision

Creates one private Chaptarr instance per Audiobookshelf (ABS) user and registers it with
the ABS Chaptarr plugin. It is one Python 3.11+ file with no dependencies beyond the standard
library, plus a compose template.

```
chaptarr-provision create alice --password-stdin    # new ABS user + private Chaptarr
chaptarr-provision create bob --abs-user-id <id>    # existing ABS user
chaptarr-provision list
chaptarr-provision status alice
chaptarr-provision delete alice [--purge]
chaptarr-provision import-profiles current-profiles.json   # or - for stdin
chaptarr-provision serve [--listen 0.0.0.0:8790]
chaptarr-provision discover [--abs-container NAME] [--template SLUG] [--write PATH] [--import-profiles]
chaptarr-provision install-service --abs-api-key-file FILE [--image IMAGE]
```

## Quickstart (zero manual config)

Run these as root on the Docker host, with ABS, the Chaptarr plugin and an existing Chaptarr
already running:

```
sudo ./chaptarr_provision.py discover --write /etc/chaptarr-provision/config.toml --import-profiles
sudo ./chaptarr_provision.py install-service --abs-api-key-file /root/abs-admin-key
```

Then open **Settings > Plugins > Chaptarr > Users** in ABS and click **Create private Chaptarr**.

`discover` reads `docker inspect`, the plugin's settings file, the template Chaptarr's
`config.xml` and its SQLite database (read-only). It writes a complete config with mode 0600,
then prints what it found and every assumption it made. Read the **WARNINGS** block: the most
common one is a missing ABS mount for the new users' folders, and the warning gives the exact
compose line to add. Without `--write`, the config is printed with its secrets redacted.
`install-service` generates the serve token, starts `chaptarr-provisioner` on the ABS network
(no published ports), waits for `/v1/health`, then sets `provisionerUrl` and `provisionerToken`
in the plugin. Both commands are safe to re-run.

### What `discover` infers, and how

| setting | source |
|---|---|
| ABS container | `--abs-container`, or the only running container whose image or name contains `audiobookshelf` |
| `docker.network` | the ABS container's first network |
| existing profiles, shared `url`, whether `provisionerUrl` is set | `<ABS /config>/plugins/.state/chaptarr/settings.json` (unmasked on disk) |
| template Chaptarr | `--template`, else the container behind the plugin's shared `url`, else the first profile's container (hostnames are matched against container names and network aliases) |
| template API key | its `Chaptarr__Auth__ApiKey` env, else `<ApiKey>` in `<its /config>/config.xml` |
| image, PUID/PGID/TZ/UMASK | the template container |
| path templates | The template's root folders (from its profile, or from its `RootFolders` table), mapped through its mounts to host paths. The owner's username or slug in a path becomes `{slug}`. A path with no owner name is shared, so each user gets a sibling folder, `<path>-{slug}`. |
| ABS-side paths | the per-user host path mapped through the ABS container's mounts |
| downloads | A mount whose destination contains `download`. If it is shared, it becomes an extra volume and each user gets `<it>/chaptarr-{slug}`. Other bind mounts become `extra_volumes`. |
| download clients, remote path mappings, indexer secrets | the `DownloadClients`, `RemotePathMappings` and `Indexers` tables of `chaptarr.db`, opened with `mode=ro`. Secret fields are the ones the template API masks, or are picked by field name when the API cannot be reached. |
| Prowlarr | A running Prowlarr container on the ABS network that has an application pointing at the template. Its key comes from its `config.xml`. |

It cannot infer the ABS admin API key, which `install-service` takes as a file. Nor can it
read secrets when the template runs on PostgreSQL: it then writes `CHANGE-ME` placeholders and
lists them.

## What `create` does

Each step is idempotent and recorded in `<state_dir>/users/<slug>.json` (mode 0600). If a step
fails, the error is recorded (with secrets masked) and the run stops. Running `create` again for
the same user resumes from the failed step. `validate`, `container` and `wait_ready` run every
time because they are cheap and repair drift. The other steps are skipped once they are done.

| step | what happens |
|---|---|
| `validate` | Derives the slug from the username (`[a-z0-9-]`, max 32 chars, unique). Checks the docker CLI, the docker network, that the ABS key belongs to an admin, the template instance and the compose template. |
| `abs_user` | Uses `absUserId`, or creates the ABS user (`type: user`, `accessAllLibraries: false`, download allowed). The password is used once and never stored. |
| `folders` | Creates `config`, `ebooks`, `audiobooks`, `downloads/{ebook,audiobook}` from the path templates. When running as root, chowns them to PUID:PGID. |
| `api_key` | Generates a 32-hex API key and stores it in the state file. |
| `container` | Renders `<stacks_dir>/chaptarr-<slug>/compose.yaml` and a 0600 `.env` that holds `CHAPTARR_API_KEY`, then runs `docker compose up -d`. The container joins the ABS network and publishes no ports. Chaptarr reads the key from `Chaptarr__Auth__ApiKey`, which binds to `Chaptarr:Auth:ApiKey`. |
| `wait_ready` | Polls `GET /api/v1/system/status` until it returns 200 (`docker.ready_timeout`, default 180 s). |
| `chaptarr_config` | Copies quality and metadata profiles from the template (custom-format scores are carried over by built-in key or name). Creates two root folders: Ebook (`folderType` 2) and Audiobook (`folderType` 1), each with its quality and metadata profile ids. Adds the download clients with per-user `ebookCategory`/`audiobookCategory` (`chaptarr-<slug>-ebook` / `-audiobook`) and the remote path mappings. Then copies the indexers from the template, or adds a Prowlarr application when `[prowlarr]` is set. |
| `shelfmark` | If the instance has a `Shelfmark` download client and `[shelfmark.users]` maps this user, sets its `shelfmarkUserId` field. Otherwise this step is a no-op. |
| `abs_libraries` | Creates `<Name> Ebooks` and `<Name> Audiobooks` (`mediaType: book`) at the ABS-side paths and adds them to the user's `librariesAccessible`. |
| `register` | Merges the profile into `<state_dir>/profiles.json`, validates the whole set (every user needs a unique url and apiKey, and paths must be absolute with no `..`), then sends `PATCH /api/plugins/chaptarr/settings {"userProfiles": "<json>"}`. |
| `hooks` | Runs every executable in `hooks_dir` (sorted) with the result JSON on stdin. That JSON has no secrets; the instance URL is not treated as one. A failing hook is reported in the step detail but does not fail the run. |

The provisioner keeps `profiles.json` as the source of truth because ABS masks secret plugin
settings when they are read back. Before the first `create`, seed it once so the existing users
(isaac, paiten, kristina) are kept:

```
chaptarr-provision import-profiles current-profiles.json
```

The input can be the profiles object, the JSON string the plugin stores, or
`{"userProfiles": "..."}`. Imported users show up in `list` as `imported`, and `create` refuses
to overwrite them.

`delete <slug>` runs `docker compose down` and removes the user's profile from
`profiles.json` and from ABS. The data folders, the stack directory and the state file are
kept, so `create` can bring the instance back with the same key and data. `--purge` also
deletes the data folders, the stack directory and the state file. Neither form touches the ABS
user or its libraries.

## Configuration

Copy `config.example.toml` to `/etc/chaptarr-provision/config.toml` and `chmod 600` it. Every
key is documented in that file. Things to know:

* **Paths.** Each folder (`config`, `ebooks`, `audiobooks`, `downloads`) has a `host` path, a
  `container` path (inside the user's Chaptarr) and, for the libraries, an `abs` path (inside
  the ABS container). Paths accept `{slug}` and `{username}`. The plugin profile uses the
  container-side ebook and audiobook roots.
* **Secrets.** Any secret can be inline or in a `*_file`. Chaptarr returns provider secrets
  (download client passwords, indexer keys) masked as `********`, so they must be given in the
  config: `[download_clients.fields]` and `[indexer_secrets."<indexer name>"]`. When a masked
  value would be copied, the run stops and the error names the missing field.
* **Download clients.** Field names are the camelCase provider field names, as the API shows
  them. qBittorrent and SABnzbd have `ebookCategory` and `audiobookCategory`. Remote path
  mappings are attached to a download client because Chaptarr requires `downloadClientId`.

## Install

There are two ways to run it. Running it as a container is recommended for `serve`.

### A. As a container (recommended)

Add this to the stack that runs ABS, so that it shares the ABS network:

```yaml
services:
  chaptarr-provisioner:
    image: ghcr.io/pace-1/chaptarr-provisioner:develop
    container_name: chaptarr-provisioner
    restart: unless-stopped
    command: ["serve"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
      - /etc/chaptarr-provision:/etc/chaptarr-provision:ro
      - /var/lib/chaptarr-provision:/var/lib/chaptarr-provision
      - /opt/chaptarr-stacks:/opt/chaptarr-stacks
      # Mount every host data root at the SAME path it has on the host (see below).
      - /srv/chaptarr:/srv/chaptarr
      - /srv/media/users:/srv/media/users
      - /srv/downloads:/srv/downloads
    # no ports: reachable only on the internal network
```

Docker resolves compose bind mounts on the **host**, but the provisioner creates folders and
writes `compose.yaml` inside its own container. Mount the stacks dir and every data root at
the identical path on both sides. Otherwise the folders end up inside the provisioner and the
user's container mounts empty host directories.

The container runs as root because it drives the Docker daemon through the socket.

One-off commands run the same image:

```
docker exec -i chaptarr-provisioner chaptarr-provision import-profiles - < current-profiles.json
docker exec chaptarr-provisioner chaptarr-provision list
```

### B. Directly on the host

```
sudo install -d /opt/chaptarr-provision /etc/chaptarr-provision
sudo cp -r chaptarr_provision.py templates /opt/chaptarr-provision/
sudo ln -s /opt/chaptarr-provision/chaptarr_provision.py /usr/local/bin/chaptarr-provision
sudo cp config.example.toml /etc/chaptarr-provision/config.toml && sudo chmod 600 /etc/chaptarr-provision/config.toml
```

The host needs Python 3.11+ and the docker CLI with the compose plugin. Container DNS names do
not resolve on the host, so set `docker.provisioner_url = "inspect"`: the provisioner then
reaches each instance at the container IP that `docker inspect` reports. To run `serve` on the
host, listen only on the Docker network's gateway address (for example
`--listen 172.18.0.1:8790`) and firewall that port from everything except that network.

## How ABS reaches the provisioner

Configure the ABS Chaptarr plugin's provisioner URL as `http://chaptarr-provisioner:8790`, the
container name on the shared network. Set its token to the same value as `server.token`.
Generate the token with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`.
`serve` refuses to start without a token of at least 16 characters.

**Never publish this service through the reverse proxy or a host port.** It holds the Docker
socket (root on the host) plus the ABS admin key. Only the ABS container should be able to
reach it.

## HTTP API (`serve`)

Every request needs `Authorization: Bearer <token>`, compared in constant time. Responses are
always JSON. API keys and passwords are never returned.

| method | path | response |
|---|---|---|
| GET | `/v1/health` | `{ok, version}` |
| GET | `/v1/users` | `[{absUserId, username, slug, state, url, createdAt, error?, lastJobId?}]`, with `state` one of `pending`, `running`, `provisioned`, `failed`, `deleted`, `imported`. `lastJobId` is the latest job this server process ran for that user, so a UI can reattach to its progress. |
| GET | `/v1/users/{slug}` | the user's state (steps, ids, errors), without the API key |
| POST | `/v1/users` | body `{absUserId?, username, password?}` returns `202 {jobId}`. Without `absUserId` the ABS user is created and `password` is required. A POST for a username whose job is still queued or running returns that job. Otherwise a new job starts and resumes from the saved state. |
| GET | `/v1/jobs/{jobId}` | `{jobId, state: queued\|running\|done\|failed, steps: [{name, state, detail?}], result?, error?}` |
| DELETE | `/v1/users/{slug}?purge=false` | `{slug, purged, deregistered, removedPaths, error?}`, or `409` while a create job runs for that user |

Jobs run one at a time on a background thread. Job records live in memory: after a restart,
a re-POST resumes from the state file. Errors return `400` (bad input), `401`, `404`, `409`
(busy) or `502` (a downstream call failed).

## Hooks

Put executables in `hooks_dir` (default `/etc/chaptarr-provision/hooks.d`). After a successful
`create`, each one receives this on stdin:

```json
{"absUserId": "...", "username": "alice", "slug": "alice", "url": "http://chaptarr-alice:8789",
 "container": "chaptarr-alice",
 "ebook": {"path": "/books/ebooks", "libraryId": "...", "label": "Alice Ebooks"},
 "audiobook": {"path": "/books/audiobooks", "libraryId": "...", "label": "Alice Audiobooks"}}
```

## Development

```
python3 -m unittest discover provisioning/tests
ruff check --config provisioning/ruff.toml provisioning
```

The tests run fake Chaptarr and ABS HTTP servers in-process, plus a fake docker runner. They
need no Docker and no network access. CI (`.github/workflows/provisioner.yml`) runs them on
Python 3.11 and 3.12. On pushes to `develop` it also publishes
`ghcr.io/pace-1/chaptarr-provisioner:develop` and `:sha-<short>`.
