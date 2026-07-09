# Runtime control plane

Hermes treats `runtime-registry.yaml` as operator-owned, secret-free control
plane data. Per-profile `config.yaml` remains user/runtime configuration; it
does not authorize a process to claim a service, port, bot platform, or the
machine-wide Kanban dispatcher.

The default path is `$HERMES_ROOT/runtime-registry.yaml`. Override it with
`HERMES_RUNTIME_REGISTRY`. The machine-readable schema is
`hermes_cli/schemas/runtime-registry.schema.json`.

```yaml
schema_version: 1
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
  coder:
    role: internal_worker
    home: /absolute/path/to/profiles/coder
```

Secure defaults are deliberate:

- A missing or invalid registry authorizes no dispatcher.
- Internal workers cannot own service labels, ports, platforms, bot
  fingerprints, or the dispatcher.
- Ports and service labels are unique; at most one dispatcher is allowed.
- Bot fingerprints, when used, must be keyed HMAC-SHA256 fingerprints. Raw
  tokens and ordinary token hashes do not belong in this file.

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
