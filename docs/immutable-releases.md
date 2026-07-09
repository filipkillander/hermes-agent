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
