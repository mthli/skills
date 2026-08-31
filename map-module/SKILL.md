---
name: map-module
description: |
  Create, fully refresh, or incrementally maintain a source-verified subsystem architecture map under `.codex/maps/`. Use when the user invokes `$map-module` or `/map-module`, asks to map, document, understand, or refresh a subsystem, or project instructions require targeted maintenance after implementation changes invalidate mapped facts. Do not use for a single-file question that leaves the existing map accurate. Authorizes bounded, read-only explorer subagents for research and verification.
---

# Map Module

Produce a “how it works today” snapshot. Keep decision history in the paired
`.codex/decisions/<module-id>.md`; do not turn the map into a design proposal or changelog.

Read `references/report-contracts.md` before dispatching research or verification tasks. Use its
report contracts and final map template.

## Choose the mode

- Use **full mapping** for a new map, an explicitly requested full refresh, broad architectural
  changes, an unverified existing map, or a change whose impact cannot be bounded confidently.
- Use **targeted refresh** when an existing verified map is being maintained after a bounded code
  change and only a limited set of mapped claims may have changed.
- If a targeted assessment proves that all mapped claims remain accurate, leave the map unchanged
  and report that no refresh was needed.

## Ownership and collaboration

- Keep synthesis, verdict judgment, file edits, module registration, and the final response in the
  root agent.
- Use `explorer` subagents only for read-only code research and verification. Full mapping uses the
  research and verification waves below. A bounded targeted refresh may be verified locally; use
  explorers when the affected flow crosses concerns or repositories. Tell every explorer not to
  edit files.
- Respect the available concurrency limit. Keep one slot for the root agent, run explorers in
  waves, and reuse an explorer with `followup_task` only for a related concern.
- Make verification independent: do not let an explorer verify its own research concern. Prefer
  fresh explorers; if capacity is constrained, cross-assign unrelated claims.
- Before mapping multiple modules or a very large subsystem, state the expected concern and
  verification counts so the cost is visible.

## Phase 0: Scope and mode

1. Resolve `<module-id>` and the subsystem root. Derive them from the request and repository when
   safe; ask only if different interpretations would materially change the map.
2. Read `.codex/MODULES.md`, the paired decision file if present, the current map if refreshing,
   and directly related maps.
3. Confirm that the root exists at the current `HEAD`. If it is missing, inspect Git history and
   symbol moves to distinguish a deleted historical subsystem from a renamed successor. Do not map
   a historical commit or switch module ids without user confirmation.
4. Inspect the root with `rg --files`, class declarations, constructors or registration points,
   public entry points, core state fields, and file sizes. Skim key files to confirm boundaries.
5. Choose full mapping or targeted refresh using the criteria above. If the current task's change
   boundary cannot be separated from unrelated working-tree changes, do not guess; use full mapping
   or report the unresolved boundary.
6. For full mapping, split by concern, not by file. Name specific classes, methods, states, and
   flows in each focus:
   - small subsystem (up to about 5 files): 3 research concerns;
   - medium subsystem: 4 concerns;
   - large subsystem or god-files: 5–6 concerns.
7. Build a short shared context containing the module purpose, root, core files, known related
   maps, and any external dependency source supplied through the task or environment.

## Targeted refresh

Skip the full research and adversarial-verification phases only when the targeted-refresh criteria
are met.

1. Use the current task and its staged and unstaged diff to identify the implementation change
   boundary. Preserve unrelated user changes.
2. Compare that boundary with the current map and list every potentially affected claim about
   responsibilities, key types, public entry points, lifecycle or data flow, inbound or outbound
   dependencies, invariants, bugs, open questions, and verification residuals.
3. Reopen the relevant source and verify every affected claim plus its immediately adjacent edges.
   Inspect dependency source supplied through the task or shared context when required. If required
   external source is unavailable, retain a concise residual under `To verify` instead of guessing.
   Do not infer that a claim is unchanged solely because its named file was untouched.
4. Escalate to full mapping if the affected claims span most sections, reveal an undocumented
   subsystem boundary, depend on an unverified baseline, or the internal impact cannot be checked
   from available project source. Missing external source alone is not a reason to escalate.
5. If no documented claim changed, do not edit the map.
6. Otherwise, patch only the affected statements and sections. Preserve the previous full
   `Verified` line and add or replace this line immediately after it:

   ```markdown
   > Maintained: YYYY-MM-DD (targeted verification: <bounded change summary>)
   ```

7. Keep evidence out of the map, run the validation steps below, and report which mapped claims
   changed. Do not commit or push unless the user explicitly asks.

## Phase 1: Full research

1. Dispatch one explorer per concern, in bounded waves, using the reader task contract.
2. Require first-hand source inspection and evidence as repository-relative `path:line` in the
   research report. Evidence is for synthesis confidence and must not enter the final map.
3. Collect every report. If a reader fails, cover the gap locally or mark the concern for
   extra first-hand verification; never synthesize a failed concern as established fact.
4. Synthesize a baseline map using the final map template. Merge overlaps, distinguish public
   names from class or package names, and place uncertainty only under `Open questions` or
   `To verify`.
5. Write or update `.codex/maps/<module-id>.md` with `apply_patch`.

## Phase 2: Full adversarial verification

1. Select checkable claims from the baseline:
   - state transitions and lifecycle edges;
   - public contracts and entry points;
   - routing, error codes, defaults, feature gates, and implementation differences;
   - invariants, suspected bugs, and all inferred or unverified assertions.
2. Use 8–10 claims for a small subsystem, 12–15 for a medium subsystem, and at least 15 for a
   large subsystem. Weight failed-reader and state-machine concerns more heavily.
3. Group claims into independent verifier assignments by concern and dispatch bounded explorer
   waves using the verifier task contract. Require one verdict per claim and fresh source reads.
4. Judge the returned evidence yourself. Treat low-confidence or unsupported verdicts as
   unresolved even if an explorer labels them confirmed.

## Phase 3: Full patch

Apply verdicts as follows:

- `confirmed`: promote important facts into the body and remove matching `To verify` items.
- `refuted` or `partial`: correct the body with `correctedStatement`.
- `external_unverifiable`: retain a concise residual under `To verify`.
- `isBug: true`: add one impact-oriented item under `Confirmed bugs / technical debt`.

Add or replace `> Verified: YYYY-MM-DD (verification summary)` only after the full verification
pass, and remove a stale `> Maintained:` line. Keep genuinely open design or intent questions under
`Open questions`. Do not claim full verification if assignments failed; state the residual
explicitly.

Use `apply_patch` for edits. Match punctuation exactly when replacing existing text. For large
section rewrites, anchor on unique headings and fail on ambiguous matches.

## Phase 4: Register, connect, and validate

1. If the module is new, add it to the matching section of `.codex/MODULES.md` only after the map
   is verified. Keep the registry near its documented size and describe the verified boundary.
2. Check whether the root `AGENTS.md` or `AGENTS.override.md` tells Codex to resolve affected module IDs,
   read the matching map and decision files before editing, and conditionally run a targeted map
   refresh when implementation changes invalidate mapped facts. If those rules are missing or only
   mention decisions, do not edit the instruction file. Tell the user that `$commit-context` will
   propose the canonical Knowledge Loop Conventions upgrade when the map is staged for commit. If
   `$commit-context` is unavailable, warn that the map will not be consumed automatically.
3. Confirm that the final map:
   - follows the reference template;
   - contains no source `path:line` citations;
   - separates facts, confirmed bugs, open questions, and external residuals;
   - pairs to the same module id under `.codex/decisions/`.
4. Run `git diff --check` and inspect the focused diff for the map and registry.
5. Do not commit or push unless the user explicitly asks. Mention confirmed risks that belong in
   a future Decision block.

## Failure handling

- If all readers fail, stop before writing a map and report the research blocker.
- If one reader fails, recover through local inspection or extra verifier coverage and disclose
  any remaining gap.
- If dependency source cannot be found, record what source is missing; do not guess or mark it
  confirmed.
- If the requested root exists only in history, stop after identifying the likely successor and
  ask whether to map the historical snapshot or the current subsystem.
- If code and an existing map disagree, trust current source, update the map, and retain historical
  rationale only in the decision file.
