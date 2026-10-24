# About this fork

This is a maintained fork of [Chaptarr](https://github.com/Chaptarr/chaptarr) for a self-hosted, multi-user setup alongside the Audiobookshelf plugin fork at `git.banditserver.com/isaac/audiobookshelf_Plugins`.

## Baseline

- Forked from upstream `develop` at `e67374f` ("Use the SDK's built-in Source Link"), version **0.9.965**.
- Upstream is not tracked automatically. Upstream changes come in by cherry-pick only.

## What comes in from upstream

- **Bug fixes** (upstream PRs or issues): taken in after review. Each one needs a regression test.
- **Enhancements and new features**: only after the owner signs off on each one.

## Roadmap

1. Baseline and CI on the self-hosted ci-linux runners
2. Bug-fix batch: upstream fix PRs, ABS-1 search crash, upstream bug issues, and replacing ImageSharp 3.x (unpatched advisories suppressed in `src/Directory.Build.props`)
3. Native Shelfmark indexer and download client (ebooks and audiobooks, per-user destinations, mapped-user auth, series-pack grouping)
4. One-step user provisioning (one Chaptarr instance per user), then the prod v1 release

## CI and releases

- GitHub (`PACE-1/chaptarr`) is the source of truth: branches, PRs and merges happen there. GitHub Actions are not used.
- Forgejo (`isaac/chaptarr`) runs all CI on the `ci-linux` runners. `sync-from-github.yml` copies every GitHub branch and tag into it every 10 minutes (Forgejo-only branches are left alone), and `ci.yml` runs on every synced branch.
- A `v*` tag (or a manual run) publishes `git.banditserver.com/<owner>/chaptarr` for linux/amd64 after every check passes.
- The in-app update check reads releases from `PACE-1/chaptarr`, never from upstream.
- Every change is verified on **librarytest** before it is promoted to prod.
