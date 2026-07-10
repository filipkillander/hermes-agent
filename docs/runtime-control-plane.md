# Runtime control plane

Hermes treats `runtime-registry.yaml` as operator-owned, secret-free control
plane data. Per-profile `config.yaml` remains user/runtime configuration; it
does not authorize a process to claim a service, port, bot platform, or the
machine-wide Kanban dispatcher.

The default path is `$HERMES_ROOT/runtime-registry.yaml`. Override it with
`HERMES_RUNTIME_REGISTRY`. The machine-readable schema is
`hermes_cli/schemas/runtime-registry.schema.json`.

```yaml
schema_version: 2
profiles:
  primary:
    role: external_gateway
    home: /absolute/path/to/profiles/primary
    service_label: ai.hermes.gateway-primary
    port: 8642
    health_url: http://127.0.0.1:8642/health/detailed
    dispatcher: true
    allowed_platforms: [telegram, discord]
    required_platforms: [telegram]
    bot_fingerprints:
      telegram: hmac-sha256:<64 lowercase hex>
    release_revision: release-2026.07.10
    domain: general
    can_delegate_to: [assistant, workers]
    can_create_boards: true
    default_board: primary
  assistant:
    role: external_gateway
    home: /absolute/path/to/profiles/assistant
    service_label: ai.hermes.gateway-assistant
    port: 8643
    domain: smart_home
    can_delegate_to: []
    can_create_boards: false
    default_board: home-automation
  coder:
    role: internal_worker
    home: /absolute/path/to/profiles/coder
    domain: worker
    can_delegate_to: []
    can_create_boards: false
```

Secure defaults are deliberate:

- A missing or invalid registry authorizes no dispatcher.
- Internal workers cannot own service labels, ports, platforms, bot
  fingerprints, or the dispatcher.
- Ports and service labels are unique; at most one dispatcher is allowed.
- Delegation and board-management authority is explicit and defaults closed.
- Every external gateway has an identity-scoped `default_board`; the stable
  launcher refuses stale mappings and exports it as `HERMES_KANBAN_BOARD`.
- Bot fingerprints, when used, must be keyed HMAC-SHA256 fingerprints. Raw
  tokens and ordinary token hashes do not belong in this file.

## Schema v1 → v2 cutover

The registry reader is fail-closed and accepts exactly one schema version.
Upgrade code and data in this order; reversing the first two steps takes every
registry-gated capability offline:

1. Build and fully test an immutable candidate whose Python understands v2,
   while the live operator registry remains v1.
2. Promote that candidate through the normal release transaction. Use the
   candidate Python for preflight; do not ask the old current Python to parse
   v2 data.
3. Atomically replace `runtime-registry.yaml` with the validated v2 document.
4. Verify the new registry revision and authority matrix using the promoted
   release Python, then restart each affected gateway through the identity-aware
   coordinator so postflight proves the same registry revision.
5. On failure, restore the byte-for-byte v1 registry snapshot before rolling
   the release pointer back. Never leave v2 data behind a v1 current pointer.

The live registry must never be written to v2 before the v2-capable candidate
is current. Registry mutation and gateway restart are separate owner-approved
operations; a release build alone does neither.

## Readiness contract

`/health/detailed` returns `runtime_identity`, `ready`, and
`readiness_failures`. Readiness requires a registry-verified external gateway,
`gateway_state=running`, successful count-only external-secret bootstrap, every
required platform connected, and every configured bot fingerprint matching.
Operators must
compare profile, service label, port, registry revision, config revision, and
optionally release revision to their expected values; HTTP 200 alone is only
liveness.

## Restart coordinator

Run the stateless process through the same release environment as Hermes:

```text
python -m hermes_cli.restart_coordinator primary
```

The coordinator locks per profile, validates config, proves service PID = port
owner = health PID, defers active work, records at most two attempts per 30
minutes, delegates restart to launchd, and requires six stable postflight
probes, with bounded exponential backoff for
failed probes. It never signals a PID or kills a process found only by port. If a
listener cannot prove the configured identity, the operation is rejected for
operator investigation.

An intentional current/previous promotion or rollback changes the desired
`release_revision` before restarting the still-old process. Use the narrow
transition gate for that one operation:

```text
python -m hermes_cli.restart_coordinator primary --allow-release-transition
```

The flag ignores only `code_revision_mismatch` during preflight. Profile,
registry, config, service/PID/port identity, secret/platform readiness, and
active-work checks remain strict. Postflight always requires the desired
registry revision, so a release that fails to activate cannot pass promotion.

`/health/detailed` is secret-free and authless only when the actual TCP peer is
loopback (`127.0.0.1` or `::1`); forwarded headers are ignored. Non-loopback
peers still require the API bearer. The local coordinator therefore does not
inherit `API_SERVER_KEY` or another gateway credential.

Secret-source readiness is exported as state plus counts for enabled, applied,
failed, and missing-required items. Secret names and values never enter runtime
status. A failed fetch/policy remains retryable inside the process but makes
readiness fail closed.

Bot identity uses keyed HMAC-SHA256. The local key defaults to
`$HERMES_ROOT/control-plane/bot-fingerprint.key`, must contain at least 32 bytes,
and must be mode 0600. Registry generation and gateway runtime use the same
key. If the key is missing or too open, no fingerprint is emitted and readiness
fails closed; raw tokens and ordinary token hashes are never persisted.
