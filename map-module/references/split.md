# Splitting a Module Map

Split one module into a narrower parent and one or more child modules when the user asks, for
example because its map no longer fits in one Read. A split moves verified facts and the boundary
that owns them; it does not research the subsystem again. Do the work locally; use explorers only
when a child needs its own full mapping.

## Contents

- Size target
- Condense or split
- Choose the cut
- Draft and measure
- Write the child maps
- Narrow the parent map
- Repoint references and the registry
- Validate
- Hand off

## Size target

Aim for maps that each fit in one Read, with room left for a full refresh, which has added about
20–25% to a map. Read's limit is counted in tokens, and bytes per token fall as identifier density
rises: maps have ranged from about 2.8 bytes per token for prose to about 2.3 for maps dense with
backticked names. Judge a dense map by tokens, not bytes. To measure, Read a scratch file that
concatenates the map with other maps until it exceeds the limit; the notice that refuses or
truncates it reports the file's total tokens. Do not take the notice's line estimate as a second
data point.

## Condense or split

- If the map is only a little over, condense first: keep `To verify` to unverified items, give
  each gap in a long gap list one line, describe key types by their role instead of listing fields,
  and drop sentences that restate their heading.
- Do not rewrite into short controlled-English sentences to save space; repeated subjects and
  vertical lists made one map about 15% longer.
- Split when condensing cannot reach the target or when a separable part makes up much of the
  bulk.

## Choose the cut

Judge each candidate part by these signals:

- It owns files, or a contiguous section of a file that the registry can name.
- It is a separate subsystem in the reference implementation or domain the project follows.
- It rarely changes together with the rest. List the commits that touched the candidate's files
  and count how many also touched the rest of the module, leaving out sweeping commits (renames,
  formatting, comment rewrites) that touched a dozen or more files.
- It leaves the parent one coherent story. Do not cut a mechanism that runs through most of the
  parent's entry points, since most changes would then read both maps; accept a parent above the
  target instead and record that as a tradeoff.

Propose the cuts with estimated sizes and get the user's approval before writing.

## Draft and measure

Estimating sizes by sorting sections and bullets into the two sides by keyword underestimates
what stays in the parent: shared invariants, dependency lines and bridging sentences stay behind or
get repeated on both sides. Earlier splits left the parent 3–8 KB larger than estimated. Draft the
child maps and the narrowed parent, then measure; if the parent is still over, propose another cut
instead of condensing facts away.

## Write the child maps

- Use the final map template. Move facts with their wording where possible; rewrite only sentences
  that leaned on the parent's context.
- Move each bug, open question and `To verify` item together with the facts it concerns.
- Record the contract between child and parent under both sides' `Inbound` and `Outbound`.
- On the `See` line, list the parent's decisions that concern the moved part, by ID, and say that
  they stay in the parent's snapshot:

  ```markdown
  > See `.claude/decisions/<child-id>.md` for the paired decision history (not yet created; decisions from before the split are D1, D13 and the page-table half of D4 in `.claude/decisions/<parent-id>.md`).
  ```

  Do not copy or move those decisions. They keep the parent's `MODULE` tag, and `/distill-module`
  indexes decisions by that tag, so an entry moved to the child returns to the parent on its next
  distill and the child loses it.
- Write the `Verified` line from the parent's mapping and do not carry over a `Maintained` line:

  ```markdown
  > Verified: YYYY-MM-DD (split out of the <parent-id> full mapping of YYYY-MM-DD; the boundary was checked against the source at the split)
  ```

  Facts inherited through a split were checked as part of the parent, not on their own. If the
  parent's map rests on an unverified baseline, or the user wants the child checked independently,
  run a full mapping of the child after the split and replace this line.

## Narrow the parent map

- Remove the moved content and update every section it touched: the root and the list of what lies
  outside it, key types, entry points, data flow, dependencies (now including each child),
  invariants, bugs, open questions and `To verify`.
- Where the parent's changes must still obey a rule a child now owns, such as recording what the
  child acts on, keep one invariant that states the duty and names the child map.
- On the `See` line, say that the snapshot still holds the decisions on the moved parts from before
  the split, naming the children.
- Keep the `Verified` line. Rewrite the `Maintained` line under the targeted-refresh rules, with
  the split as the last area changed.

## Repoint references and the registry

- Search the repository for the parent id and for the moved concepts: other maps, the registry,
  docs, and source comments that name a map. Repoint every statement that attributes a moved fact
  or file to the parent. Expect several other maps to change.
- In `.claude/MODULES.md`, narrow the parent's entry and register each child next to it. When the
  boundary runs inside a file, name the sections or symbols each side owns and write the parent's
  entry with `except`; the registry is then the only record of that boundary.

## Validate

- Check the boundary against the source: every file, section and symbol the registry assigns
  exists where it says, and no part of the old root is left without an owner.
- Apply Phase 4's map checks to the parent and to each child, and run `git diff --check`. If source
  comments changed, run the project's formatting checks; if nothing but comments changed, say that
  nothing was built or tested.
- Report each map's size before and after the split.

## Hand off

- Do not commit. Suggest one commit per parent, holding the parent and its children.
- For `/commit-context`, suggest one Decision per affected module. Tag the parent's Decision with
  the parent and use `SUPERSEDES` for the decision, if any, that made the moved parts part of the
  parent. Give each child a Decision of its own with what it owns, the decision IDs it inherits,
  and which changes will now read two maps.
- A child's decision snapshot is created by a later `/distill-module` run from decisions tagged
  with the child, starting with the split commit's own.
