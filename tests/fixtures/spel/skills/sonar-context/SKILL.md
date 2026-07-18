---
name: sonar-context
description: Minimal fixture skill for the #spel route-contract tests. Provides channel context read policy. NOT a real skill.
version: 0.0.1-fixture
metadata:
  hermes:
    tags: [fixture, spel-contract]
---

# sonar-context (fixture)

Fixture skill for #spel route-contract. Provides context read policy markers
so contract tests can verify skill hash/readiness via a mock skill loader.