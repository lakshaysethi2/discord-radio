# Git branches (2026-09-03 cleanup)

Read this when touching remotes, force-push, resurrecting old work, or merging GitHub history that is not on this VPS.

## What is live

GitHub has **only** `main` and `prod`. Do not recreate deleted `arena/*`, `fm/*`, `fix/*`, `scrub/*` branches.

**This VPS is source of truth.** Live radio is Drive via WebDAV + archive leftovers (`FILE_PROVIDER_ORDER=webdav,archive`). GitHub `main` was force-pushed to VPS tip `5da5a15`. GitHub default branch is `prod`; it is ruleset-protected (PR required, **squash only**, no force-push, no deletion). Synced via squash PR #25 — **same tree as `main`** (`42606d2`).

## What was not merged, and why

Old GitHub `main` had a torrent/aria2 stack (PR #3) that this radio does not run. **Do not merge it** — it would regress the working WebDAV path. Same for unused HTTP media provider. Recover only if captain asks, from tags (not branches):

- `archive/origin-main-pre-vps-sync` — pre-force-push GitHub `main` (torrents/aria2)
- `archive/http-media-provider` — PR #13 HTTP provider, not in `FILE_PROVIDER_ORDER`
- `archive/radio-ci-e2e` — PR #18 self-hosted CI; activation recipe already in `ci/github-actions.yml`

Closed as superseded/equivalent: PRs #13, #18, #21 (scrub already `f497992`), #24 (Drive-as-primary is how prod already runs).

## Ops notes

- Push from this host: GitHub SSH key is denied; use `gh` HTTPS.
- Do not `git push -f` `prod` (ruleset).
- Do not raise the 10s silence SLA.
- Cleanup gate (no container rebuild): docker pytest 539 passed, ruff check clean, `tvbot-{bot,dashboard,file-provider}` left healthy.
