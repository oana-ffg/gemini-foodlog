# Event scenario ground truth and scoring v1

This contract makes FoodLog's event evaluation repeatable without pretending that
one exact sentence is the only correct model answer. It applies to reviewed stills,
Veo sequences, and consented real-camera sequences. It does not turn synthetic data
into evidence of real-world accuracy.

Scenario records validate against
[`event-scenario-ground-truth-v1.schema.json`](../contracts/event-scenario-ground-truth-v1.schema.json).
Only a record whose `review_status` is `human_approved` and whose human-review record
names the reviewer, time, and decision notes may contribute to reported evaluation
results. A draft remains useful for implementation but cannot be counted as approved
ground truth.

## What every scenario records

- **Provenance:** synthetic still, synthetic video, or consented real capture.
- **Ordered observations:** immutable relative path, SHA-256, camera, and time offset.
- **Event boundaries:** the exact observations belonging to each expected event,
  start/end offsets, and a scenario-specific tolerance.
- **Authorized context:** only evidence intentionally available to the agent at that
  time, with an explicit source kind and availability offset.
- **Primary and acceptable outcomes:** activity kind, accepted semantic labels,
  allowed confidence, components, ingredient concepts, and preparation methods.
- **Required evidence:** visual and contextual facts that should support the answer.
- **Question policy:** required, allowed, or forbidden, including the specific
  competing concepts and why the answer would matter.
- **Prohibited claims:** unsupported conclusions with either hard-fail or ordinary
  deduction severity.

Paths are repository-relative and cannot contain parent traversal. Scenario IDs,
observation IDs, event IDs, context IDs, and evidence IDs are unique within their
scope. Every referenced file and hash is checked before a replay.

## Scoring one scenario

Score the final persisted inference—not a raw provider response—on a 100-point scale.
Engineering failures such as auth bypass, cross-account access, missing durable state,
or an invalid output contract fail the run before semantic scoring.

| Dimension | Points | Measurement |
| --- | ---: | --- |
| Event grouping and boundaries | 20 | 12 points for maximum-matched observation-set overlap; 8 for start/end offsets within the recorded tolerance. Missing and extra events receive zero for the unmatched slot. |
| Activity and food outcome | 25 | 5 for activity kind, 8 for a primary accepted label (6 for an explicitly acceptable alternative), 6 for required components, and 6 for required ingredient/preparation concepts. |
| Evidence fidelity | 20 | 10 for required visual-evidence coverage, 5 for authorized context with correct provenance, and 5 for deductions that link to the evidence they actually use. Unsupported observations do not earn credit. |
| Uncertainty and alternatives | 20 | 8 for allowed confidence, 6 for preserving material alternatives, and 6 for avoiding prohibited claims. A merely plausible context item must never be presented as visual proof. |
| Question decision and quality | 15 | 7 for asking or abstaining according to policy, 5 for naming the recorded competing concepts rather than requesting a meal label, and 3 for linking the question to evidence and an accepted impact. |

### Event matching

The replay manifest maps each immutable production capture ID back to its scenario
observation ID; score only after that mapping and its image hash are verified. Pair
predicted and expected events to maximize the Jaccard overlap of their source
observation-ID sets. Include unmatched predicted and expected events as zero-score
pairs so splitting or merging is not hidden by averaging only successful matches.
For a matched pair, the boundary score is the mean of the start and end scores:

```text
boundary point score = max(0, 1 - absolute error / tolerance)
```

When tolerance is zero, an exact boundary earns one and any difference earns zero.
This calculation uses captured timestamps, not the later worker execution time.

### Semantic matching

Matching is case-insensitive and ignores surrounding whitespace, but it does not use
an embedding or let a model grade itself. An accepted label or term must be listed in
the reviewed scenario. If a sensible synonym is missing, a human may amend and
re-approve the ground truth before rescoring every affected run; the evaluator may
not silently grant a one-off exception.

The primary outcome earns full label credit. An explicitly acceptable alternative
earns six of eight label points because it is supportable but weaker than the reviewed
best interpretation. It still must preserve the required uncertainty and question.
Unknown activity earns activity/outcome credit only when it is itself recorded as an
accepted outcome; it is not a universal safe fallback.

### Questions

- `required`: the inference must contain one focused question whose candidate concepts
  cover the recorded distinction and whose impact is accepted by the scenario.
- `allowed`: a focused question may appear without penalty, but needless or generic
  wording earns no question-quality points.
- `forbidden`: any event question loses all 15 question points.

“What meal were you cooking?”, “What ingredient was that?”, and equivalent requests
for the user to label the whole event are hard failures. A useful question distinguishes
specific supported hypotheses, such as chicken versus red meat, and is asked only when
the answer could change the journal, trigger relevance, or reusable household knowledge.

## Hard failures

A scenario fails regardless of its numeric score if any of these occurs:

- cross-account evidence, history, or learned knowledge influences the result;
- synthetic observations enter a real household's learning state;
- a claim marked `hard_fail` is asserted as fact or used to raise confidence;
- the system asks a generic meal-labeling question;
- a non-cooking or unknown event exposes a confirmation action for a nonexistent meal;
- the reported result omits, rewrites, or cannot retrieve its immutable evidence and
  inference revision.

Hard failures remain visible alongside the numeric breakdown. They are never averaged
away across a dataset.

## Dataset reporting

Report each scenario separately, then summarize by source kind and scenario family.
For a repeated probabilistic run, report the median, full range, hard-failure count,
and exact model/prompt/tool versions. Do not publish a single “accuracy” percentage
unless the reviewed dataset is large and representative enough to support that claim.

The hackathon release uses these interpretation bands only as evaluation shorthand:

- **90–100, no hard failure:** strong pass;
- **75–89, no hard failure:** usable but review the deductions;
- **below 75 or any hard failure:** fail and investigate.

These are product-evaluation thresholds, not calibrated probabilities or medical
performance claims. Synthetic, Veo, and real-camera results remain visibly separate.

## Initial draft scenarios

- [`synthetic-cat-negative.v1.json`](../tests/fixtures/scenarios/synthetic-cat-negative.v1.json)
  expects a likely non-cooking event, no meal question, and no invented food.
- [`distant-meat-with-recent-chicken.v1.json`](../tests/fixtures/scenarios/distant-meat-with-recent-chicken.v1.json)
  expects an uncertain concrete guess, preserves chicken versus red-meat ambiguity,
  and requires a focused question because recent chicken context makes the distinction
  relevant without proving the image's contents.

Both remain `draft` until Oana approves their visual ground truth and intended product
behaviour; the approval record will preserve her decision and time. That approval is
the remaining human portion of EVAL-001.
