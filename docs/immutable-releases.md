# Immutable Hermes releases

`hermes_cli.release_manager` stages tracked Git content into a sealed release,
builds dependencies before sealing, verifies every file by SHA-256, and moves a
profile's `current` pointer atomically. It never edits the source checkout and
never runs shell command strings.

## Layout

```text
$HERMES_HOME/releases/<release-id>/
$HERMES_HOME/runtime-links/<profile>/current
$HERMES_HOME/runtime-links/<profile>/previous
$HERMES_HOME/release-snapshots/<snapshot-id>/
$HERMES_HOME/release-locks/<profile>.lock
```

Release directories are mode 0555. Regular files are 0444 and executable files
0555. Runtime/snapshot/lock parents are 0700. Snapshot files are 0600.

## Stage and verify

Use a committed integration ref. Dirty or untracked source files are never
included.

```bash
python -m hermes_cli.release_manager --home /Users/ai/.hermes stage \
  2026.07.10-self-healing.1 \
  --source /path/to/integration-worktree \
  --ref codex/hermes-self-healing \
  --build-json '["uv","sync","--frozen","--no-dev"]'

python -m hermes_cli.release_manager --home /Users/ai/.hermes verify \
  2026.07.10-self-healing.1
```

Staging rejects tracked `.env`, `auth.json`, `bws_cache.json`,
`credentials.json`, `secrets.json`, and `client_secret_*` entries. A release
larger than the configured budget fails before it receives a public release
name.

## Inactive update build phase

`hermes_cli.immutable_update_builder` is the fail-closed build half of a future
automatic updater. It has no promotion, rollback, restart, scheduling, or
retention command. It only:

1. takes a global non-blocking build lock;
2. requires a clean repository root whose `HEAD`, explicit ref, and explicit
   full expected commit are identical;
3. compares that commit with the verified `current` manifests for the named
   profiles;
4. runs the fixed repository `scripts/run_tests.sh` focus harness in a clean
   environment;
5. revalidates the ref and worktree, then stages with
   `uv sync --frozen --no-dev --no-editable --extra messaging`;
6. rewrites uv-generated absolute staging shebangs to the final release path
   before the manifest is sealed;
7. verifies the sealed manifest, size, write modes, non-editable imports,
   messaging imports, and the installed `hermes --help` entrypoint.

It writes only states, counts, commit/digest values, and output byte counts to
`$HERMES_HOME/release-status/immutable-update-builder.json` (mode 0600). Raw
test/build output, environment values, file paths, and credential names are not
persisted. A matching current commit is a successful no-op.

The command below is documentation only; no job invokes it until a separate
rollout approves and installs a scheduler:

```bash
python -m hermes_cli.immutable_update_builder build RELEASE_ID \
  --home /Users/ai/.hermes \
  --source /path/to/clean/integration-worktree \
  --ref refs/heads/codex/hermes-self-healing \
  --expected-commit FULL_40_CHARACTER_COMMIT \
  --profile spark --profile igor --profile lumi
```

Staging is not promotion. A staged release cannot affect a running gateway;
the canary/restart coordinator and atomic promotion remain separately approved
operations.

## Registry identity versus release intent

`RuntimeRegistry.revision` is a canonical hash of the ownership boundary:
profile homes, roles, labels, ports, platform allowlists/requirements,
dispatcher ownership, and keyed bot fingerprints. Per-profile
`release_revision` is deliberately excluded from that one hash because it is
validated independently as `code_revision`.

This separation is required for safe promotion and rollback. Changing only
the desired release must not make the still-running, otherwise correctly
identified process fail the registry-identity preflight. Any change to a
label, port, home, role, platform boundary, dispatcher, or bot fingerprint
still changes the registry identity and blocks mutation until an explicit
bootstrap has established the new boundary.

## macOS Keychain bootstrap helper

Headless macOS profiles may set
`secrets.bitwarden.access_token_keychain.helper_path` to a private, regular,
owner-executable helper. Hermes verifies that the path is absolute, is not a
symlink, belongs to the current user, and has no group/other permission bits.
The helper's stderr is never copied into a secret-source error.

The helper binary is a small Keychain trust anchor. Replacing or rebuilding it
requires coordinated BWS token rotation; it must not be rewritten by normal
Hermes release staging. Profile Keychain account names are versioned so a
failed rotation cannot overwrite the last working bootstrap.

## Secret-free rollback snapshot

Only explicit regular files below `HERMES_HOME` may be included. Secret-store
filenames fail closed. This is update metadata, not full disaster recovery.

```bash
python -m hermes_cli.release_manager --home /Users/ai/.hermes snapshot \
  pre-self-healing-20260710 \
  --include /Users/ai/.hermes/runtime-registry.yaml \
  --include /Users/ai/.hermes/profiles/lumi/config.yaml
```

Profile config may be snapshotted only after a verifier proves that it contains
secret references, not literal values. Full disaster recovery belongs in a
separate encrypted backup system.

## Promotion and rollback

Preflight runs before any link changes. Postflight runs after `current` moves;
a failing postflight atomically restores the old pointer. Probe arguments are
JSON arrays and execute without a shell or inherited secret environment.

```bash
python -m hermes_cli.release_manager --home /Users/ai/.hermes promote spark \
  2026.07.10-self-healing.1 \
  --preflight-json '["/path/to/coordinator","spark","--preflight-only"]' \
  --postflight-json '["/path/to/coordinator","spark","--verify-only"]'

python -m hermes_cli.release_manager --home /Users/ai/.hermes rollback spark \
  --postflight-json '["/path/to/coordinator","spark","--verify-only"]'
```

The lifecycle coordinator, not the release manager, drains or restarts a
gateway. A launcher/LaunchAgent should resolve the profile's `current` link and
execute that release's own `.venv/bin/python`.

## Retention

Retention is dry-run unless `--apply` is explicit. Every `current` and
`previous` target is protected regardless of age. At least two additional
unlinked releases must be retained.

```bash
python -m hermes_cli.release_manager --home /Users/ai/.hermes prune --keep 2
python -m hermes_cli.release_manager --home /Users/ai/.hermes prune --keep 2 --apply
```

## Required rollout order

1. Fake/loopback profile.
2. Spark.
3. Igor.
4. Lumi.

Each real promotion requires identity-aware config/port/PID/profile checks,
required platform readiness, six stable probes, and a working `previous`
rollback target. The legacy live-checkout updater must remain disabled until a
real rollback drill has passed for Spark and Igor.
