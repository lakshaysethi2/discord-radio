# Branch policy

## One stable branch, at most one working branch

- **`prod`**: the only stable and default branch. Keep its protection enabled;
  do not force-push or delete it. Promote reviewed changes through a squash PR.
- **One fixes/features branch**: currently `arena/6d24f54e-discord-radio`.
  All pending work shares this branch and one PR into `prod`. Arena sessions
  have fixed branch names; this one fills the working-branch role.
- **No parallel `main`, per-issue branches, or backup branches.** Preserve old
  tips as archive tags, verify them remotely, then retire superseded branches.
- A new working branch replaces (does not accompany) the old one. Archive any
  unmerged tip first. Do not resurrect a whole archived tree to recover one fix.

After a squash merge, Git history may diverge even when trees match. Compare
content and open PRs before cleanup; never assume “not an ancestor” means
“contains unshipped features.” Never delete a branch that changed during review.

## October 2026 consolidation

| Former branch | Archived tip | Archive tag |
| --- | --- | --- |
| `main` | `b5c4516e6eb0992023849f09ed06039f22caa75b` | `archive/2026-10-14-main` |
| `fix/voice-reconnect-race-prod` | `074bf27e3127d4d4ad5cdeee4e2cf57771399280` | `archive/2026-10-14-voice-reconnect-race-prod` |

PR #35 is closed as superseded by [draft PR #36](https://github.com/lakshaysethi2/discord-radio/pull/36).
Its reconnect guard and tests are incorporated on the working branch,
with additional fixes for the real discord.py disconnect API and outstanding
connection tasks. Useful recovery behavior from `main` is integrated without
removing `prod`'s flap detector or weakening the 10-second health threshold.
The archived `main` also retains its complete incident notes and audit history.
See [the review](review-2026-10-14.md) for intentionally deferred behavior.

Older archive tags remain available:

- `archive/origin-main-pre-vps-sync`: historical torrent/aria2 stack, not the
  current WebDAV deployment; do not merge wholesale.
- `archive/http-media-provider`: parked HTTP-provider work from PR #13.
- `archive/radio-ci-e2e`: previous self-hosted CI work from PR #18.

## Release gate

1. Docker lint, format check, and full pytest suite pass. CI activation is
   currently blocked by App workflow permissions; see `ci/README.md`. Until
   activated, run the Docker gates manually.
2. Review the combined PR against `prod`; ensure unrelated archived work has
   not been reintroduced.
3. Squash merge through the protected PR process. Merging is not deployment.
4. Deploy deliberately, then verify real voice playback with listeners,
   pause when empty, track advance, reconnects, and Gatus health. Unit tests
   and a connected voice socket alone do not prove audio is reaching listeners.
