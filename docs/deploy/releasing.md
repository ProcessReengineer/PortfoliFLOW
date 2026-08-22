# Releasing PortfoliFLOW

Maintainer-facing. The mechanism this document describes is decided in
ADR-0124 §4; the ADR is the authority, this is the operating procedure.

## The ritual

Cutting a release is one manual step. Everything else is CI.

1. Set `version = "<calver>"` in `pyproject.toml` — calver without a `v`
   prefix, e.g. `2026.09.0`.
2. Commit that change.
3. Create an annotated tag whose name is the identical string:
   `git tag -a 2026.09.0 -m "2026.09.0"`.
4. Push the tag: `git push origin 2026.09.0`.

Nothing else is done by hand. In particular the `stable` branch is never
advanced manually as part of a normal release.

## What the tag triggers

Pushing the tag starts two workflows on the same trigger. They are independent
— a red run in one neither blocks nor undoes the other.

**`promote-stable.yml`** does two things in order:

1. **Guard.** Asserts that `pyproject.toml` carries exactly the version named
   by the tag.
2. **Promote.** Force-advances the `stable` branch to the tagged commit.

`stable` is what the README clone command resolves, so a release becomes
publicly current at the moment this workflow succeeds.

**`release-assets.yml`** applies the same version guard, then creates the
GitHub Release entry for the tag with generated notes — only if none exists
yet — and attaches `install.sh.sha256`, the checksum of the tagged commit's
`scripts/install.sh` (ADR-0124 §3). That is the file README Option B tells a
downloader to compare against; it matches what `stable` serves, because
`promote-stable` puts `stable` on the very commit the checksum was taken from.
Rewriting or expanding the generated release notes afterwards is optional and
manual — nothing in the ritual or in the installer depends on their content.

Pre-release tags — anything containing a hyphen, e.g. `2026.09.0-rc1` — are
ignored: both jobs are skipped, `stable` does not move and no release entry is
created. Publish release candidates freely; they never reach the people
following `stable`.

## When the version and the tag disagree

The guard fails, the workflow stops, and `stable` stays exactly where it was.
This is the intended outcome: drift between the tag and the packaged version
becomes a release-blocking failure rather than something a user discovers
later.

To recover: correct `version` in `pyproject.toml`, commit, then move or
re-create the tag on the corrected commit and push it again (a re-pushed tag
needs `--force`, and the workflow runs afresh on the new ref).

## One-time setup the workflow depends on

These are operator actions outside the repository. The workflow assumes all
three exist; step-by-step instructions live in the operator's own notes, not
here.

- **Deploy key.** A write-capable deploy key for this repository, stored as
  the repository secret `STABLE_DEPLOY_KEY`. The workflow checks out over SSH
  with it and pushes with it — its `GITHUB_TOKEN` is read-only by design.
- **Ruleset on `stable`.** A branch ruleset protecting `stable` against
  force-pushes, listing that deploy key as a bypass actor. Force-pushing
  `stable` therefore remains possible only through a bypass under project-owner
  control, which is the property ADR-0124 §4 asks for.
- **The branch itself.** `stable` must exist before the first workflow run
  that would advance it. Done once, from the then-current release tag:
  `git push origin '2026.08.0^{commit}:refs/heads/stable'`.

## Manual promotion

Documented fallback for when the workflow cannot run — Actions disabled or
degraded, the deploy key rotated out, a release that must go out while CI is
broken. The project owner advances the branch directly, using their admin
bypass on the ruleset:

```bash
git push --force origin '<tag>^{commit}:refs/heads/stable'
```

The `^{commit}` suffix is required: release tags are annotated, so the tag
name refers to a tag object, and a branch must point at a commit. Without the
suffix the push is rejected with an unhelpful `[remote rejected]`. (The
workflow itself is unaffected — it pushes `$GITHUB_SHA`, which is always a
commit.)

Check by hand what the workflow would have checked: the tagged commit's
`pyproject.toml` must carry exactly `<tag>` as its version.

## Weekly installer CI

`.github/workflows/installer.yml` guards the installed-from-scratch path
rather than a release: `shellcheck` over `scripts/*.sh`, an end-to-end
`install.sh` run on Linux — asserting the `.env` mode, a green
`portfoliflow status`, `--doctor`, the exit-13 refusal on a second run, and a
transcript free of the owner password — and, on macOS, that every script
parses and runs under a real `/bin/bash` 3.2 with BSD userland. It runs on
pull requests touching the installer's inputs, on `main`, and every Monday at
05:17 UTC.

The weekly run is the load-bearing one: installer rot comes from the outside
world, not from commits to this repository. A red Monday against an unchanged
installer means a runner image, a Compose provider or a base image moved, and
that is the same breakage a user following the README would hit next.

The macOS **end-to-end** install is not covered here and stays a manual
acceptance test (deviation D1): GitHub's arm64 macOS runners do not support
nested virtualisation, so `podman machine start` cannot succeed on them. Run
it from the operator's own acceptance checklist before a release that changed
`scripts/install.sh`.
