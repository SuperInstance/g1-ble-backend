# Play-test: the first real run

Every automated test in this repo drives `_FakeClient`. Until this document,
the real pipeline — real HTTP, real inference, real model quirks — had never
been executed end to end. Everything below came from actually running it.

Model: `qwen2.5-coder:7b` on an RTX 4050 Laptop (6 GB). No G1 hardware.

## The headline: the product is not usable while walking

```
play-testing the real pipeline against qwen2.5-coder:7b
 median 6.71s   mean 8.95s   max 19.55s   min 2.39s
 p95 15.77s  ->  NOT USABLE WALKING for a walking wearer
```

Stage 1 is **85% of that** (23.87s of 28.14s across three queries), running at
**37.7 tok/s — 27ms per token**. It generates 133–287 *words* to feed a
**120-character** display.

### A README claim that was wrong

The README said *"Latency claims were ~5–7× optimistic. Real: 110 ms sustained
warm, 3.55 s cold."*

Those are `winnow`'s numbers, and **winnow is not on the display path.** For
the flow a wearer actually invokes, the real figure is ~7s. The README row is
now corrected in place rather than quietly deleted.

## The two-stage split costs 13x and buys nothing measurable

`DESIGN.md` justifies the split with: *"asking one model to do both degrades
the reasoning."* Never tested. Now it is, at n=18 over a corpus with
one-hop, computational, and trap questions:

| arm | median | p95 | max | fits | answers the question | median chars |
|---|---|---|---|---|---|---|
| **two (shipped)** | 5.36s | 11.87s | 16.74s | 17/18 | 16/18 | 68 |
| **one (direct)** | **0.40s** | 0.81s | 1.31s | **18/18** | **16/18** | 35 |
| two, short stage 1 | 6.82s | 17.10s | 17.12s | 17/18 | 15/18 | 62 |

**13.4x faster, identical answer relevance, a better fit rate.** The shipped
claim is falsified on this corpus.

Two honest caveats, neither of which is a reason to keep a 13x cost:

- n=18, and "answers the question" is a crude string proxy, not a judge. It is
  enough to falsify a claimed *benefit*; it is not enough to certify a
  replacement.
- One-stage is **much terser** — 35 median chars against 68. That is ~1.5 of 5
  display lines. Whether that is good restraint or under-using the display is a
  design question this corpus cannot answer.

### What this run does NOT settle: fabrication

The first run printed `fabricated numbers: 8` for two-stage and `0` for
one-stage, which reads as a decisive win. **It is a measurement artifact** and
must not be reported as a result: with a single call the model's output *is*
its own source, so checking numbers against it is tautologically zero. The
script now prints `n/a` for that arm.

This matters beyond the arithmetic. Stage 2 exists partly because it is "a
structural filter, not a summariser" — the load-bearing defence against a
compressor inventing a number while shortening. Deciding whether that defence
is worth 13x needs a ground-truth corpus, not a self-comparison. **That is the
experiment that should come next.**

## Bugs found by running it

### 1. Setting `OPENAI_MODEL` left stage 2 on `gpt-4o-mini` → 404

The very first command run:

```
LLM config: stage1=qwen2.5-coder:7b  stage2=gpt-4o-mini
probe failed: 404 - model 'gpt-4o-mini' not found
```

`build_llm_config` took the compressor from the preset's default and never fell
back to the primary model, while `ensure_models` only auto-discovers when it is
*falsy* — so it stayed wrong forever. Anyone pointing at a local server hit a
404 about a model they never asked for. Fixed: a redirected `base_url` now
falls back to the primary model.

### 2. A Python exception repr was rendered onto the wearer's lenses

With stage 1 failing on a 401, the glasses showed:

```
LLM error. stage 1 (m) f
ailed: Error code: 401 -
 {'error': {'message':
```

Three of five lines of exception repr, broken mid-word, with the only useful
part (401) buried on line two. A walking wearer glancing at that gets nothing.
The lens now gets a fixed short line (`No answer. Model auth failed.`) and the
full diagnostic stays on the terminal.

### 3. The drill-down tap was a silent no-op

`detail_generation` defaults to `off`, so `more()` recorded a `drill_miss` and
returned the same card **without writing to the lenses** — while the REPL
printed the unchanged text back as if something had happened. The most-promised
gesture did nothing and said nothing. It now renders a one-line notice.

### 4. Ctrl-C at the prompt hung the process

`prompt_user` used `asyncio.to_thread(input)`. SIGINT left the worker blocked
in C-level `input()`, and `asyncio.run` blocks forever joining the default
executor. Reproduced: **exit 124, hung.** Now a daemon thread — **exit 0.**

### 5. `:text` overflowed the display with no warning

`:text` is a deliberate raw path, so the text is still not silently budgeted —
but it overflowed five lines with no word on the terminal. An operator
reproducing an overflow now gets told.

## Open, and honestly so

- **Stage 1 still consumes `preferences()`.** The unguarded imperative string
  shapes what gets reasoned about, not just what gets compressed.
- **Glanceability gate false positives.** It fired on real output that clearly
  contained concrete tokens (`digit=0 proper=0` on "Blue light scattered more
  efficiently"). Its false-positive rate on real model output is unmeasured.
- **`_chat` discards `usage`.** Token counts had to be estimated from string
  length, which is why "how many tokens did stage 1 burn" started as an
  approximation rather than a measurement.
- **Cold start still folds into warm numbers.** The model was resident for all
  of the above.
- **No G1 hardware.** Every BLE path remains unproven on device, and nothing in
  this document changes that.

## Reproduce

```bash
export OPENAI_BASE_URL=http://127.0.0.1:11434/v1 OPENAI_API_KEY=ollama
export OPENAI_MODEL=qwen2.5-coder:7b
.venv/bin/python experiments/playtest.py      # latency, budget, truncation
.venv/bin/python experiments/stage_split.py   # one stage vs two, n=18
```

Both degrade to `SKIP` and exit 0 when the model is unreachable, so neither is
a network dependency.