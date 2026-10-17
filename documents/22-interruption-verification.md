# 22 — Verifying an interruption without asking: what the literature does and does not have

*Date: 2026-10-16. Follows [21](21-phase3-findings.md).*

---

## Why this was run

documents/21 ended with the labelling blocker stated plainly: four of five
policy axes have no data source, because nothing asks the question they need
answered. The obvious next move was to design a mechanism against the code,
which two agents did. This is the third track — the literature — because a
mechanism designed in a vacuum tends to reinvent a known failure and call it
insight.

A note on method, because it bounds the claims: **no full text was read.**
Findings come from indexed titles, abstracts and bibliographic records via
OpenAlex, arXiv and Crossref. DBLP blocked automated access and Semantic
Scholar throttled parallel calls. Absence of evidence below should be read as
"not findable with these tools", not as proof of nonexistence.

## The headline: the literature is mostly empty here, and that is the finding

This is the part worth stating first, because the instinct on a blank search
result is to search differently.

| searched for | result |
|---|---|
| "Microsoft Band" AND notification | **zero** records |
| `"delayed feedback"` AND notification (arXiv) | **zero** records |
| "shadow deployment" as a method name | **zero** in HCI/ML |
| "silent A/B" as a method name | **zero** peer-reviewed |
| Google Glass notification-value studies | none found |
| a validated instrument for "notification usefulness" | **none exists** |
| any evidence for a numeric interrupt budget | **none** |

So this project is not behind on prior art. There is very little prior art.
Four of those nulls are load-bearing, and two of them changed what the code
does.

---

## Change 1 — a link drop is CENSORING, not delay

The most useful result in the whole track.

The theory of delayed credit assignment is solid and transferable:
**Pike-Burke, Agrawal, Szepesvári & Grünewald (ICML 2018)**, *Bandits with
Delayed, Aggregated Anonymous Feedback* ([arXiv:1709.06853](https://arxiv.org/abs/1709.06853)),
shows regret grows only by an additive factor in the expected delay when
rewards arrive late. That result assumes the label **eventually arrives**.

A G1 link drop is not delay. For that window the label never arrives at all:
the wearer could not have acted on a push that never rendered. Pike-Burke's
bound does not cover non-returning labels — **censored** observations are a
different problem, and the bandit literature has a name for what you must not
do with them.

So `main.py --review` now **refuses to label a decision made while the glasses
were off the wearer's face**, explains why, and leaves the row unlabelled:

```
#2  queue digest   feed
     Card pushed while link was DOWN
     NOT LABELABLE — the glasses were off your face or the link was down
     when this was judged, so 'no reaction' carries no information. Left
     unlabelled on purpose: a missing label is not a negative one.
```

Scoring it `0` would inject a systematic false negative into the one axis
whose entire job is precision — and it would be a *manufactured* one, which is
the kind that survives review because the ledger looks well-populated.

This is why the `observable` column from documents/21 turned out to matter:
it is the difference between a missing label and a false one.

## Change 2 — `expand` measures legibility, not value

The mechanism review proposed `expand` ("tapped for more") as a strong label
signal. The literature says be careful, and the reason is specific to this
hardware.

On a **120-character display**, tapping for more often means *"I didn't
understand this"* — which is a statement about **legibility**, not about
whether the content was worth interrupting for. The closest prior art is
**Janaka et al. (ISMAR-Adjunct 2023)**, *Minimizing Attention Costs of Visual
OHMD Notifications*
([10.1109/ismar-adjunct60411.2023.00036](https://doi.org/10.1109/ismar-adjunct60411.2023.00036)),
which is about attention cost of optical-headset text specifically.

`EVENT_WEIGHTS["expand"] = 0.25` already encodes this by making `expand` the
weakest positive signal and `star` the strongest, which is the right ordering.
The refinement is that **`expand` should never be written into the `label`
column** — it belongs to the content/formatting axis, not the value axis. That
is a constraint on the next design, recorded here so it is not rediscovered.

## Change 3 — there is no evidence for ≤2/hour, and the framing should change

Nothing in the literature supports a numeric notification budget, and nothing
compares a hard cap against a cost-based policy. The available evidence points
the other way:

- **Iqbal & Bailey (HCI 2006)**, *Leveraging characteristics of task structure
  to predict the cost of interruption*
  ([10.1145/1124772.1124882](https://doi.org/10.1145/1124772.1124882)) —
  interruption cost is a function of whether the task is bounded or unbounded
  and well- or poorly-defined. **A constant cap is wrong by construction**;
  it is the cost of interrupting a well-defined 30-second task that differs
  from the cost of interrupting open-ended work.
- **Fischer et al. (CHI 2010)** ([10.1145/1851600.1851620](https://doi.org/10.1145/1851600.1851620))
  and **Turner, Allen & Whitaker (Personal Mobile Computing 2017)**, *"Reachable
  but not receptive"* ([10.1016/j.pmcj.2017.01.011](https://doi.org/10.1016/j.pmcj.2017.01.011))
  — receptivity depends on content, time and current engagement.
- Users set the budget themselves, via snooze and DND: **Weber et al.
  (MobileHCI 2018)**, *"Snooze!"*
  ([10.1145/3229434.3229436](https://doi.org/10.1145/3229434.3229436));
  **Auda et al. (CHI 2018 EA)**; **Pielot & Rello (CHI 2015 EA)**,
  *The Do Not Disturb Challenge*
  ([10.1145/2702613.2732704](https://doi.org/10.1145/2702613.2732704)).

The canonical framing is decision-theoretic rather than numeric:
**Horvitz, Jacobs & Hovel (UAI 1999)** balance "the context-sensitive costs of
deferring alerts with the cost of interruption" — push when expected alert
value exceeds expected interruption *cost*.

**What this means for the project:** `MAX_PUSHES_PER_HOUR` should be described
as a **safety rail, not an objective.** It is the right thing to ship first,
because a system with no ceiling is worse than a system with a crude one, and
because its cost is a few missed pushes while its benefit is bounded
catastrophe. But the learned quantity should eventually be a conditional cost
model, with the rail preventing runaway rather than doing the deciding.

The current design already has the shape of this: the budget is a constraint
checked *before* evidence (documents/88681d9 / policy.py), and G2 eligibility
is a deterministic rule rather than a score. What is missing is the conditional
cost term, and no hardware means it cannot be fitted yet.

## The structural conflict nobody can design around

The literature produces a constraint that no mechanism can satisfy:

**At ≤2 pushes/hour you cannot both collect real labels and learn per-event
from the same stream.**

The reason is an identification problem, not an engineering one. If you push
only when confident, the never-pushed events are a biased sample of the
population you care about, and no estimator recovers their value without an
overlap/propensity assumption. **Wang, Agarwal & Dudík (ICML 2017)**,
*Optimal and Adaptive Off-policy Evaluation in Contextual Bandits*
([arXiv:1612.01205](https://arxiv.org/abs/1612.01205)), establish a **minimax
lower bound** for the agnostic case — the case with no reward model, which is
exactly the state this project is in.

With the budget at ~4/day, the overlap region is on the order of 20–25
pushes/day in the best case and far fewer in practice. That is not enough to
both estimate value and exploit it.

**So: shadow evaluation and per-event learning are different phases, not
simultaneous activities.** The mechanism differs completely between them, and
the project has to choose which one it is in rather than trying to run both.

This is a stronger constraint than "labels are hard to get." It says the
*shape* of the learning problem changes depending on whether the gate is
already pushing, and the two shapes need different machinery.

## Ranked shortlist of usable signals

1. **Time-to-first-touch, with dismiss/confirm as ordinal outcomes.** Highest
   volume per push, zero incremental wearer cost (reuses the existing
   vocabulary), seconds of latency. Failure modes: `expand` leaks legibility
   in (above); and **mask out any window containing a link drop**.
   Grounding: Turner et al. 2017; Janaka et al. 2023.
2. **A randomised explicit audit prompt on a small fraction of pushes.** Ranked
   high not because it is a good per-event signal but because **nothing else
   is interpretable without it and no calibration study exists to borrow.**
   Must be *randomised*, not sampled by need — **Shiffman & Stone (2007)**,
   *Ecological Momentary Assessment*
   ([10.1146/annurev.clinpsy.3.022806.091415](https://doi.org/10.1146/annurev.clinpsy.3.022806.091415)),
   establishes that asking changes what you measure. Sampling by need
   reintroduces the bias the prompt was meant to remove.
3. **Subsequent re-request / downstream completion.** The only signal measuring
   whether the interruption was correct *hours later*, which is what the axes
   conceptually want. Zero cost. Minutes-to-hours latency — squarely the
   delayed-credit regime where Pike-Burke et al. say the penalty is additive.
   **Limitation: only exists for re-requestable content.** A calendar invite is
   re-requestable; a Slack message is not.
4. **`confirm`, as a weak positive only.** "Sufficient" is a low bar and
   habit-prone. Never ground truth.
5. **`star` — excluded as a label.** Too sparse (a few per day) and it
   conflates present value with deferred intent ("remind me later"). Keep as a
   feature, not a label.

## Explicitly not built, and why

- **No randomised audit prompt yet.** It is the right instrument, and it is a
  product decision: it puts a word on the lenses occasionally, which is a
  small violation of silence-by-default. That is the user's call, not a
  default this project should set silently.
- **No conditional cost model.** Correct shape per Iqbal & Bailey and Horvitz,
  un-fittable without hardware and without labels.
- **No shadow mode for `detail_generation` yet.** The delayed-feedback theory
  supports it and `documents/20` named it as a way out of the deadlock, but the
  identification caveat above applies to it as much as to anything else.

## Unverified items, listed rather than buried

- The widely cited **~23-minute task-resumption figure** is attributed to
  **Mark, Gudith & Klocke (CHI 2008)**, *The cost of interrupted work: more
  speed and stress* ([10.1145/1357054.1357072](https://doi.org/10.1145/1357054.1357072)).
  **The number itself was not verified in the text** and should be checked
  before it is ever quoted in this repository.
- **Yuan et al. (CIKM 2022)**, *Offline Reinforcement Learning for Mobile
  Notifications* — the closest industrial analogue, DOI not independently
  verified.
- Full text of nothing above was read. Titles and abstracts only.

## What this changes about the plan

Nothing here argues for building the labelling UI faster, and one thing argues
against the current default. The project already has the review loop; the
research says the *instrument* matters more than the volume:

1. Decide which **phase** the system is in — collection or exploitation. The
   identification result makes them mutually exclusive, and the current code
   is a little of both.
2. Treat **censoring** as a first-class concept, now enforced in `--review`.
3. Describe the budget as a **safety rail**, and stop implying 2/hour is
   derived from anything.
4. Keep `expand` out of the value axis.
5. If a randomised audit prompt is wanted, it is a **product decision** about
   spending one word on the lenses occasionally. Worth asking the wearer.