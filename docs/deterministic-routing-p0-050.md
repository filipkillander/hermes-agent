# Deterministic Routing (P0-050)

Per-turn injection receipts and fail-closed guards for system-prompt assembly.

## Overview

Hermes assembles the system prompt from three tiers (stable, context,
volatile).  Before P0-050, all material-loading failures were **fail-open**
(silent fallback / skip): missing SOUL.md fell back to a hardcoded identity,
missing USER.md/MEMORY.md was silently skipped, and no skill was guaranteed to
be present.  There was also no per-turn visibility into what the model
actually received.

P0-050 adds:

1. **Per-turn injection receipt** — SHA-256 hashes of each tier component,
   stored on `agent._last_injection_receipt`.
2. **Fail-closed options** — when `required_material_fail_closed: true` in
   config, missing required materials raise `BlockedRequiredMaterial` instead
   of silently falling back.
3. **Required-skills enforcement** — skills listed in
   `agent.required_skills` must be present and parseable.

All fail-closed behaviour is **opt-in** via config.  When
`required_material_fail_closed` is absent or `false` (default), behaviour is
unchanged — full backward compatibility.

## Configuration

Add to `config.yaml` under the `agent` section:

```yaml
agent:
  # Skills that must be present and parseable at agent init.
  # If any is missing and required_material_fail_closed is true,
  # agent init raises BlockedRequiredMaterial.
  required_skills:
    - formatting-harness

  # When true, missing required materials (SOUL.md, USER.md, MEMORY.md,
  # required skills) raise BlockedRequiredMaterial instead of silently
  # falling back.  Default: false (backward compatible).
  required_material_fail_closed: true
```

## Injection Receipt

After every `build_system_prompt_parts()` call, the following structured
receipt is stored on `agent._last_injection_receipt`:

```json
{
  "version": 1,
  "persona_sha256": "abc123...",
  "user_context_sha256": "def456...",
  "memory_sha256": "789abc...",
  "skills_sha256": "def012...",
  "stable_sha256": "...",
  "context_sha256": "...",
  "volatile_sha256": "...",
  "persona_present": true,
  "user_context_present": false,
  "memory_present": true,
  "skills_present": true
}
```

The receipt is lightweight (7 × 64-char hex strings + 4 booleans) and never
blocks prompt assembly.

## BlockedRequiredMaterial

When fail-closed is enabled and a required material is missing, a
`BlockedRequiredMaterial` exception is raised:

```python
from agent.required_material import BlockedRequiredMaterial

try:
    # ... agent init ...
except BlockedRequiredMaterial as e:
    print(e.material)  # "SOUL.md", "USER.md", "skill:formatting-harness"
    print(e.reason)    # diagnostic string
```

## Files Changed

- `agent/required_material.py` (new) — `BlockedRequiredMaterial`,
  `compute_injection_receipt()`, `check_persona_fail_closed()`,
  `check_memory_fail_closed()`, `enforce_required_skills()`
- `agent/system_prompt.py` — receipt computation in
  `build_system_prompt_parts()`, SOUL.md fail-closed guard
- `agent/agent_init.py` — `_p050_config` setup, memory fail-closed check,
  required-skills enforcement
