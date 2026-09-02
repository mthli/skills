# Map Module Report Contracts

Use these contracts in explorer tasks. Keep evidence in task reports; remove all `path:line`
citations from the final map.

## Contents

- Reader task
- Verifier task
- Final map template

## Reader task

Provide the explorer with:

- module id and repository-relative root;
- one concern key and a focused list of classes, methods, states, or flows;
- shared context and related maps;
- an explicit instruction to inspect source read-only and not edit files.

Require this report:

```markdown
## Concern
<key>

## Responsibility
<one concise paragraph>

## Key types
- `<type>` — <role>

## Entry points
- <entry point>

## Data flow and lifecycle
<ordered flow or state transitions>

## Dependencies
- In: <caller or upstream>
- Out: <callee, service, SDK, or external dependency>

## Invariants and gotchas
- <claim>

## Open questions
- <question whose intent cannot be answered from source>

## Unverified
- <claim that still lacks source>

## Evidence
- `<repo-relative-path>:<line>` — <what this proves>
```

Ask the explorer to ground every factual claim in evidence, distinguish direct facts from
inference, search only relevant slices of god-files, and return `none` for empty sections.

## Verifier task

Provide numbered claims with a verification hint. Tell the explorer to reopen source, default to
skepticism, inspect any external dependency source supplied in the shared context, and return one
item per claim:

```markdown
### <claim-id>
- verdict: confirmed | refuted | partial | external_unverifiable
- finding: <what source shows, with repo-relative path:line evidence>
- correctedStatement: <ready-to-paste sentence without line numbers>
- isBug: true | false
- confidence: high | medium | low
- missingSource: <required only for external_unverifiable>
```

Interpret verdicts strictly:

- `confirmed`: source proves the claim as written.
- `refuted`: a material part is wrong.
- `partial`: the core is right but scope, condition, or transition is incomplete.
- `external_unverifiable`: required source is genuinely unavailable from the project and supplied
  external context.

## Final map template

```markdown
# <module-id> Map
> Static understanding snapshot, not a decision history.
> See `.claude/decisions/<module-id>.md` for the paired decision history (note when it does not yet
> exist).
> Verified: YYYY-MM-DD (verification summary)

## Responsibilities

## Key types

## Public entry points

## Data flow / lifecycle

## Dependencies (inbound / outbound)

## Invariants and gotchas

## Confirmed bugs / technical debt

## Open questions

## To verify
```

Omit `Confirmed bugs / technical debt` when none are confirmed. Omit `To verify` when no external
or failed-check residual remains. Preserve the other headings so maps stay comparable.
