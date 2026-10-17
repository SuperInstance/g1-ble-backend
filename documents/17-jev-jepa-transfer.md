# JEV and JEPA — what SuperInstance has already measured, and what transfers

Two independent reviews of `SuperInstance/AI-Writings` (1,014 JEV hits, 588
JEPA hits across ~3700 files), plus `SuperInstance/jev-quilt`. This is a
transfer note: what is genuinely measured elsewhere, what it means for the
glasses backend, and what it changes.

The headline is a deflation. **The AI-Writings corpus contains far more
architectural intent than measured evidence.** Stated up front so the rest is
read with the right priors.

---

## 1. Auth — the blocker, resolved

My 401 is the *same* failure already diagnosed in that corpus.
`situations/dispatch-ledger.csv:80` (entry `d078`, 2026-09-28):

> the live JEV call now returns **401 authentication_error** from
> api.typesafe.ai — reproduced identically from this session's TYPESAFEAI_KEY
> AND from the deployed Pages Function's bound secret (**same 108-char key**,
> both rejected; a malformed request gets a DIFFERENT 403, so this is a real
> credential rejection, not a schema issue). Root cause: this exact key was
> found HARDCODED in that repo's PUBLIC git history; rotation required.

Two things follow, both actionable:

1. **The key format is `apikey_…`.** The scrub receipt
   (`jev-quilt/docs/receipts/004-key-scrub.json`) verifies with
   `grep apikey_ across repo`, and the only surviving string in the repo is the
   placeholder `apikey_xxx`. The file I was given
   (`TYPESAF_…_…_…_<36>=<64>`, 125 chars) is **not** a TypeSafe key — the
   shape is a secret-manager reference, not a bearer token. That is why it 401s
   under every header style.
2. **Env var.** The SDK documents `TYPESAFE_API_KEY`; every live client uses
   `TYPESAFEAI_KEY`. `jev.py` now accepts both.

**Still needed from you:** a real `apikey_…` value from
https://console.typesafe.ai/keys. Everything is built and waiting.

---

## 2. JEV — measured, and several things I had assumed

### Fast enough, but nobody timed it

`theory/paper-jev.md:61-62` quotes **p50 150ms, p99 800ms** — vendor figures,
third-party cited. **No latency was ever measured** in the corpus: zero hits for
`latency_ms|elapsed|took .*ms` across `jev-kat`, `jev-fold`, `weakest-claim`,
`situations/play`. So the number to design against is 800ms at p99, not 150ms.

Cost, likewise all vendor: **$0.042 / 1M input tokens**, output free
(`paper-jev.md:63-64`); ~450 in / ~40 out tokens per call is *asserted* and never
confirmed against a real `usage` block.

### The finding that kills my original plan

I had been hoping Jev's calibration would let a threshold **retire the 299-push
requirement**. It cannot. From `labs/weakest-claim/README.md:17`:

> **Those are the signals, not the absolute noul level: JEV rates almost any
> absolute guarantee at about 0.1–0.35.**

Five engineering guarantees differing enormously in real quality scored
**0.11 / 0.18 / 0.32 / 0.29 / 0.28** (`README.md:56-59`). And on a
character-exact task the margin between *right* and *wrong* was **0.07**
(`S2-FINDINGS.md:37-39`). A gate needs separation far larger than that.

**So: `noul` is for ranking and direction, never an absolute precision gate.**
The 299-push requirement in `policy.py` stands, and `experiments/jev_gate.py`
already says so in its own output.

### `score` is unusable for thresholds

`labs/jev-kat/jev_kat.mjs:24-26`:

> fed numpy, a 1,285-assertion reference port, and an empty shell, it returns a
> **near-constant ~2.6 at ~0.62 confidence regardless of input. Confidence does
> not drop to signal the failure.**

Three wildly different inputs, one answer. `jev.py` uses `choice` and will
never use `score` for a gate.

### Method beats type: ask it to compare, not to rate

`JEV-FINDINGS.md:118-122`:

> **Pairwise choice ≫ independent noul-scoring.**

Measured directly (`JEV-USAGE-LOG.md:86-97`): both methods got 3/3 at argmax,
but independent nouls let **two options both clear 0.5** (Jupiter 0.52,
Saturn 0.79) — a threshold reader would accept a wrong answer. `choice`
normalises, costs 1 call instead of N, and its confidence honestly flagged the
hardest item (0.64). `jev.py` already used `choice`. Now it's justified rather
than lucky.

### Where it was actively harmful — the strongest evidence in the corpus

A 7-round ensemble study (`situations/experiments/S2-FINDINGS.md`). Round 4:

| | score |
|---|---|
| best-single | 24/24 |
| vote | 24/24 |
| **fold (JEV as judge)** | **21/24** ← *worse than both* |

All three lost items were **counting** (`"i" in indivisibility`, vowels in
`sequoia`) and **clock angles** — Jev scored a wrong minority answer above the
right majority one. The conclusion (`:151-156`):

> The Reader's Fold with a judge is only as good as **the judge's competence in
> that task's domain.** Trusting judged evidence over headcount is a win only
> where the judge is competent, and a loss where it is not.

And the design consequence (`:161-163`):

> **Two floors, kept separate — proven the hard way.** JEV (legality/confidence)
> must never be the *correctness* oracle; a real correctness floor (tests /
> ground truth) stays beside it.

The deployable prescription is the **safe fold**: use Jev `choice` except on
counting → majority vote. `safe_fold ≥ vote` in both sets, never below
(`:202-215`). "Trading volatile upside for a guaranteed floor is the correct
engineering choice for a judge with a known blind spot."

### Two things I got right by accident

- **This project has a 120-CHARACTER budget — a character-counting task, Jev's
  measured blind spot.** Competence map: counting **2/5 (0.40)**, confidently
  wrong, margins 0.52–0.76, while arithmetic/dates/geography/logic/geometry all
  scored 1.00. Separately, hyphen-spelled misspellings came back 0.82–0.89
  *true*. So `enforce_display_budget` in Python stays authoritative, and
  `jev.py` now says so in its own docstring. This would have been a
  spectacularly bad integration.
- **Degradation.** The corpus records a connector that "random-fallbacked"
  before the schema arrived — "probabilities that were costumes draped over
  ignorance". `jev.py` returns `None` on any failure. Jev was also fully down
  for an extended period (`d078`→`d095`), so transient-503 retry is in.

### Do not use the `jev-decide` package

It looks like the obvious import and is a trap:

```python
DEFAULT_ENDPOINT = "https://ai-writings.pages.dev/api/jev/decide"   # __init__.py:25
headers={"Content-Type": "application/json", "User-Agent": ...}     # no Authorization
```

It carries **no auth header**, routes through SuperInstance's own unauthenticated
Pages proxy, and that proxy returns **HTTP 200 with `CORS:*` on error** — so a
credential failure surfaces as a normal response. Hardcoded `timeout=60`, no
retries, zero error handling. Verdict from the study, which I agree with:
*reusable as a schema reference, not as transport; do not build a precision gate
on a free-tier public relay.*

---

## 3. JEPA — 95% prose, with one lesson worth taking

Stated plainly because it matters: **no model weights anywhere** (zero `.pt`,
`.safetensors`, `.onnx`), **no torch/transformers import in any `.py`**, and
1326 mentions across 261 files reduce to **one self-contained program**. There
is no V-JEPA and no inference pipeline to port. Every V-JEPA-2-in-production
claim in the Tap prose is fiction.

Also a terminology trap: "JEPA" there means three different things, and only one
is LeCun's.

The one transferable finding — and it is a *negative* one
(`labs/ml-in-quilt/README.md:115-117`):

> **Latent closeness is not product identity.** The JEPA predictor maps Z_in →
> Z_out with R² = 0.993, yet it has the *lowest* teacher-forced token agreement
> of any plan (0.772). Residual error lands on decision boundaries.

Asserted as a standing invariant in the selftest. Drawn as a rule at
`ML-IN-QUILT-ARCHITECTURE.md:242-245`: *"A JEPA predictor is therefore a draft,
never a final route."*

Two more, both measured: `spec-jepa` was **slower than fp** in real Python
execution (63.3ms vs 45.8ms) — the speed claims are modeled, not measured. And
the "48× faster / 15M params" figures are a *literature summary of someone
else's paper*, not Casey's work.

The Rosetta Stone piece (jev+jepa+rsi+gan) is an LLM competition entry, truncated
mid-sentence, in which RSI and GAN appear **only in the title**. Its companion
demolishes the framing itself:

> The framing is **inspirational substrate poetry**… It is NOT a literal claim
> to wire into the cell algebra. **Keep the metaphor; don't bind the operators.**

---

## 4. The part that stings

`documents/13-round2-claude-convergence.md:30` in *this* repo already says:

> A learned gate can never be shown to meet the target… **The 0.813 score is
> logged, not used to decide.**

That is structurally identical to the corpus's central lesson — a well-fit
scorer that does not move the decision. **The AI-Writings repo offers no new
evidence on this point; it corroborates a conclusion already paid for.**

The corollary from `ML-IN-QUILT-ARCHITECTURE.md:242-245`: a Jev or
surprise-style signal belongs *behind* the interrupt gate as a **tiebreaker
among already-whitelisted candidates**, never as the gate itself.

---

## 5. Also worth stealing: the ambiguity sentinel

From the ESP32 cell spec (`research/jev-esp32-client.md:111-113`): on network
failure it does **not** report 0, which would mean "definitely not canonical".
It reports **p = 0.5, confidence = 0.0** — "spike = 0.5 ambiguous".

That is an epistemic-status encoding, and for deciding whether to interrupt a
walking wearer it matters more than any embedding. `jev.py` now carries
`ambiguous_verdict()` for exactly this: a caller aggregating across sources
must not be able to turn "unknown" into a confident no. On this project the
suppress path is the one that is supposed to be most careful, and this is how
you keep a network failure from silently becoming one.

Companion pattern from the same doc: *"Even if every network call fails, the
cell records 'I tried, the world was silent' and sleeps."* Act on what you have;
log what you couldn't get.

---

## What to do next, in order

1. **Get a real `apikey_…` key.** Nothing else can start.
2. Run `experiments/jev_gate.py`. It reports separation and calibration
   *separately*, because only the second could authorise an interrupt — and
   per the evidence above, it probably cannot, in which case the honest
   outcome is that Jev joins the gate as a tiebreaker and the 299-push rule
   stands unchanged.
3. If it separates well, the follow-up is the **safe fold** shape: Jev
   `choice` for ranking, majority vote on anything counting-shaped, and
   deterministic code as the correctness floor. Never one, alone.

Sources are file-and-line cited above; every "measured" claim there is
Casey's own record, not the vendor page. Where the corpus merely asserts, it is
labelled asserted.