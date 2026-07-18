---
channel_id: "1509925693247459378"
channel_name: "#spel"
guild: vendelip
mode: Game Mode
---

# Vendelip #spel — Game Mode brief (fixture)

This is a minimized public brief fixture for the #spel channel route-contract tests.
It is NOT the real private brief. It exists to prove the channel-ID -> brief -> skill
contract mechanically: the brief is loaded by relative path, hashed, and surfaced by
ID/path/hash in the per-turn receipt without leaking private brief text.

## Mode

Game Mode is active for this channel. The agent should defer to required skills
for game-specific behavior and treat the knowledge root as read-only reference
material.