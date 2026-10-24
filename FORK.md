# About this fork

This is a maintained fork of [Chaptarr](https://github.com/Chaptarr/chaptarr) for a self-hosted, multi-user setup alongside the Audiobookshelf plugin fork at `git.banditserver.com/isaac/audiobookshelf_Plugins`.

## Baseline

- Forked from upstream `develop` at `e67374f` ("Use the SDK's built-in Source Link"), version **0.9.965**.
- Upstream is not tracked automatically. Upstream changes come in by cherry-pick only.

## What comes in from upstream

- **Bug fixes** (upstream PRs or issues): taken in after review. Each one needs a regression test.
- **Enhancements and new features**: only after the owner signs off on each one.
- **Database migrations**: fork migrations are numbered from 1001 up, so they can never take a number upstream later uses. FluentMigrator applies any unapplied version, so a cherry-picked upstream migration with a lower number still runs.

## Roadmap

1. Baseline and CI on the self-hosted ci-linux runners
2. Bug-fix batch: upstream fix PRs, ABS-1 search crash, upstream bug issues
3. Native Shelfmark indexer and download client (ebooks and audiobooks, per-user destinations, mapped-user auth, series-pack grouping)
4. One-step user provisioning (one Chaptarr instance per user), then the prod v1 release

## CI and releases

Everything runs on GitHub Actions in `PACE-1/chaptarr`. Forgejo isn't used until the codebase moves there.

- `build.yml` runs on every PR and push to `develop`/`main`. It covers the backend build and tests, the frontend typecheck and build, and the Postgres migration smoke test, all on Linux only, since Chaptarr ships only as a container image.
- `docker-publish.yml` publishes `ghcr.io/pace-1/chaptarr:develop` (plus `:sha-<short>`) after every green push build on `develop`. A manual run on any other branch publishes `:branch-<name>` for testing.
- `release.yml` (manual) tags the version in `package.json`, creates the GitHub release, and publishes `:<version>`, plus `:latest` from `main`.
- The in-app update check reads releases from `PACE-1/chaptarr`, never from upstream.
- Every change is verified on **librarytest** before it is promoted to prod.
