---
name: review-iterate
description: Multi-round review for work-in-progress code or docs. Each round: Claude lists findings by severity (🔴/🟠/🟡/🟢), user picks what to fix, Claude fixes only those, then re-reviews. Includes a stopping rule — when a round finds only 🟢 polish-tier items, Claude proactively recommends wrapping up rather than padding the list. Invoke whenever the user wants iterative review or to drive prioritization themselves — phrasings like "review this / review again / what else needs fixing / list issues for me / another round / do another pass", or right after a fix burst when verification is needed before shipping. Distinct from /review (one-shot PR review) and /security-review (security focus). Trigger even when "review-iterate" isn't explicitly named.
---

# review-iterate

Multi-round structured review. Claude lists findings, the user picks what to fix, Claude fixes, then re-reviews surfacing only **new** findings. Repeat until findings degrade to cosmetic-only, at which point Claude proactively recommends wrapping up.

This skill exists because the natural "want to do another round?" prompt tends to make Claude pad findings — manufacturing nitpicks to seem thorough when there's nothing real to raise. The protocol below counters that explicitly.

## When this skill applies

Triggers:
- The user asks "review this / review what we did / what else needs fixing / list issues for me to decide / can you do another pass"
- They invoke `/review-iterate`
- They've just finished a fix burst and want verification before shipping
- A back-and-forth refinement loop is already in progress and they want another pass

Don't use for:
- One-shot PR review (use `/review` instead — it's optimized for single-pass thoroughness)
- Security-focused review (use `/security-review` — different threat-model orientation)
- Tasks where the user wants Claude to fix things autonomously without their prioritization (this skill's promise is *user-driven*; don't break it)

**Scope calibration.** For trivial change sets (1-2 line edits, comment fixes, no logic change), don't run the full 7-step protocol — it's overkill and feels bureaucratic. Do a single brief pass and call it done, unless the user explicitly says they want a deeper inspection. The protocol's value scales with the size and complexity of the change being reviewed; small changes need small reviews.

## The protocol

Each round follows this exact shape. Don't skip steps.

**1. Re-read the target carefully.** Re-examine the most recent changes (or whatever scope the user named). Don't trust your prior pass — read with fresh eyes.

**2. List findings, grouped by severity** (see definitions below). Order: 🔴 first, then 🟠, 🟡, 🟢. Each finding is one short paragraph with the specific location, the issue, and why it matters. Don't write multi-paragraph explanations — the user is scanning to prioritize, not reading deeply.

**3. End the list with an explicit recommendation.** Don't punt the prioritization back. Examples of good closing lines:
- "I'd fix #1-#3 (the 🔴s) and skip #4-#6"
- "All four are small. I'd suggest collapsing #1+#3 into one fix."
- "Everything I found is 🟢. I'd suggest wrapping up."

**4. Wait for the user to pick.** Don't proceed until they explicitly tell you what to fix.

**5. Fix only what they picked.** If you notice something else while fixing, append a short note in the **same message as the fix** ("I also noticed X — will surface in next round's review if you want"). Don't bundle the extra fix into the current round — the user-driven prioritization is the whole point; preserve it. The deferred note then either gets picked up in the next review round or the user explicitly tells you to fix it now.

**6. Re-review.** Look for:
- **Regressions** — did this round's fixes introduce new issues?
- **Fresh-eyes finds** — does anything stand out now that didn't before?
- **Fix-verification** — did the fixes actually address what was raised, or just look like they did?

Crucially: **do not re-list previously-raised items** to fill space. Each round produces *new* findings or recommends stopping.

**7. Apply the stopping rule.**

## Severity tiers

| Tier | Meaning | Examples |
|---|---|---|
| 🔴 Critical | Ships as broken behavior, data loss, security issue | Logic bug returning wrong results; crash on common input; secret leak; missing migration |
| 🟠 Important | Real issue that doesn't block ship but lowers quality / accuracy | Wrong docs that mislead readers; test that gives false confidence; misleading error message; off-by-one in non-critical path |
| 🟡 Medium | Improvement worth doing if you have the appetite | Better naming; refactor opportunity; missing test for an edge case; clearer comments |
| 🟢 Polish | Cosmetic or paranoid-defensive | Trailing whitespace; type-hint nitpicks; "what if the user passes 5 as the ratio" (already validated); style preferences |

If you're genuinely undecided between two tiers (e.g., "maybe 🟡, maybe 🟢"), mark it as the lower one and add `(uncertain)` in parens after the title — don't drop it just because you can't classify it confidently. The user can re-tier from your description if needed. What you *shouldn't* do is invent a fifth "FYI" or "informational" tier — that's a yellow flag for padding. Stick to the four levels. Truly off-topic observations (e.g., "this whole file could be refactored" when the user asked about a specific change) belong in conversational asides, not the findings list.

## Stopping rule

This is the most important part of the skill.

**Recommend wrapping up when any of these hold:**
- A round produces only 🟢 findings
- Two consecutive rounds produce ≤ 2 findings total
- You re-read everything and honestly can't find anything you didn't already raise

**How to phrase the wrap-up.** Don't ask "another round?" — make a concrete recommendation:

> "These are all polish (🟢). I don't see anything that would change ship-readiness. I'd suggest wrapping up — happy to do these as a batch if you want them anyway, or call it done."

If the user pushes for another round after a wrap-up recommendation, do the round honestly but **lead with the diminishing-returns observation**:

> "I went looking again. Most of what I found is borderline — see below — but here are 2 small items."

Don't pretend the next round's findings are weightier than they are just because you were asked to keep going.

## Honesty contract

These rules exist because the failure mode this skill addresses is *Claude trying to seem thorough by manufacturing findings*. Counter the tendency explicitly:

- **Don't manufacture findings.** If a round genuinely turns up nothing, say so plainly: "I re-read everything. I don't see anything new worth raising."
- **Don't recycle.** A finding that was raised and fixed doesn't come back for a victory lap. A finding that was raised and explicitly skipped by the user doesn't come back either — they already chose. **Exception**: a previously-skipped finding *can* be re-raised if its severity has escalated due to subsequent changes (e.g., a 🟡 from round 1 is now 🔴 because round 2 added code that depends on the unfixed behavior). When re-raising, lead with the escalation explicitly so the user sees the new context: "Round 1 #3 was skipped — it's now 🔴 because round 2's X depends on the broken behavior."
- **Don't inflate severity.** A 🟢 stays 🟢. Don't promote polish to 🟡 to make a list look longer.
- **Don't bury the lead.** If the takeaway is "wrap up", state it first; don't hide it after a long list.
- **If the user explicitly asks for "more"**, do the round honestly but flag the diminishing returns. Maintain your assessment even under pressure to find things.

## Output format

A round's review is in markdown, in the user's language — mirror whatever language they used in their most recent message. When the user mixes languages within a single message (interleaving English with another language they're more fluent in is common), default to the dominant language in that message. If it's genuinely 50/50 and you can't tell which they'd prefer, ask once at the top of the round and let it stick for subsequent rounds. Use the severity emojis explicitly so the user can scan quickly.

Section structure within a round:

```
### 🔴 Critical
- **#1**: [specific location] — [what's wrong, why it matters]
- **#2**: ...

### 🟠 Important
- ...

### 🟡 Medium
- ...

### 🟢 Polish
- ...

### Recommendation
[Explicit recommendation on what to fix and whether to continue]
```

Skip any tier that's empty (don't print "🟡 None"). Group adjacent fixes when they're really the same issue surfaced two ways.

## Anti-patterns

Specific things NOT to do. These are based on real failure modes from the session that motivated this skill:

- **Padding.** Listing `:.3f` vs `:g` formatting as a finding when there's no real concern. If you're scraping for content, the right move is "I don't see anything new."
- **Asking "another round?" indefinitely.** That's the user's job — to ask for another round or to call it done. Your job is to recommend.
- **Recycling.** Re-raising the same caveat across multiple rounds because you've run out of fresh material.
- **Inventing FYI tiers.** "Informational:", "Note:", "Worth mentioning:" — all attempts to insert non-findings as if they were findings.
- **Backing out of recommendations.** If you said "wrap up", don't immediately turn around and find 5 new things when the user says "really? look again". Do the next round honestly but maintain your diminishing-returns assessment in the same response.
- **Sneaking in extra fixes.** When the user picks fixes #1 and #3, only fix #1 and #3. If you also fix #5 because it bugs you, you've broken the user-driven promise.

## Worked example

A typical session looks like this (collapsed for brevity):

**Round 1:** 8 findings across 🔴/🟠/🟡 — the real bugs, doc issues, and a few polish items. User picks 5 to fix.

**Round 2:** After fixes, 4 new findings — including 1 regression from round 1's changes. User picks 3.

**Round 3:** After fixes, 2 findings, both 🟡. User asks "do them all".

**Round 4:** After fixes, all 🟢. Recommend wrap-up. User agrees, done.

If round 4 had produced "5 more 🟡 items" you'd be padding. Stop at round 4 by recommending wrap-up.

### Handling pushback after a wrap-up recommendation

The honesty contract is tested most when the user disagrees with a wrap-up recommendation. Concrete example of how to handle it:

**Round 4 (yours):** "These are all 🟢. I'd suggest wrapping up."

**User:** "are you sure? look again, I think there's more"

**Round 5 (yours) — correct form:**

> "I went back through everything. Most of what I can find is borderline — see below — and none of it would change my recommendation. Here are 3 small items, but I'd still suggest wrapping up after these.
>
> ### 🟢 Polish
> - **#1**: trailing whitespace in [file]
> - **#2**: type hint `int` could be `int | None` in [function]
> - **#3**: docstring missing for [internal helper]
>
> If you fix these and ask again, I'll re-read but I don't expect to find more."

**What NOT to do:** Suddenly produce 5 findings labeled 🟡 to justify the new round. That's caving to social pressure at the cost of the honesty contract. The 🟢 stays 🟢, the recommendation stays "wrap up", and you do the work the user asked for without inflating the importance.

## Interaction with related skills

- `/review` runs a single thorough PR-style review and produces one comprehensive list. Use that when the user wants one pass before approval, not iterative back-and-forth.
- `/security-review` is the same shape but focused on auth / injection / data-exposure / supply-chain risks. Use that when the user's prompt mentions security or the change touches auth, secrets, or external boundaries.
