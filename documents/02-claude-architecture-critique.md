# Architecture review: G1 relay agent

## 0. Your brief says more is verified than really is

Several claims in the brief don't match the repo. They matter more than any tiering question.

- **Nothing has run on the glasses.** Your own README (`README.md:227`) says scan, connect, MTU negotiation, notifications and rendering are "Not tested … No G1 was available." The "verified, non-negotiable" hardware facts are well-sourced guesses, not measurements.
- **The new modules aren't connected to anything.** `main.py` imports only `g1_driver` and keeps its own copy of the pipeline (`main.py:371`, `:441`). `pipeline.py`, `session.py`, `memory.py` and `plugins.py` are untracked, and nothing imports them. Drill-down and the feedback loop can't be reached from the program you run.
- **The refiner can never run.** `plugins.py:161` and `:204` call `store.get_meta` and `set_meta`. `FeedbackStore` doesn't have those methods. The broad `except` hides the error, so `should_run` always returns False.
- **The 25-word budget doesn't fit the screen.** English averages about 5.7 characters per word including the space, so 25 words is about 140 characters. Five lines of 24 is 120. Your cap reliably spills onto a sixth line. Also, `g1_driver.py:1070` wraps to 24 characters but then joins the lines back together with spaces, so nothing actually enforces the 5-line limit. Measure the budget in wrapped lines, not words.
- **Two of your gestures are probably one gesture.** The community notes you cite list `0xF5 0x04` and `0x05` as triple tap, alternating each time (a silent-mode toggle). If so, "confirm" and "star" (`session.py:53`) will alternate with each triple tap instead of being two choices. The firmware may also use single and double taps itself. Capture real taps before designing around them.

So you have a four-tier architecture over a system that has never run end to end once. That's the main point of this review.

## 1. The tiering: the discriminative-first gate is premature

**The proposed Tier 0 has nothing to gate yet.** "Candidate event → worth interrupting?" assumes a stream of incoming items. The code has none: every card starts as a question you typed. You should never silently drop a question you asked. So right now Tier 0's main feature does nothing. **You haven't defined where events come from** (notifications? messages? calendar? location?), and that choice drives everything else: how many there are, how urgent, and what counts as "worth it."

**`winnow` answers a generic question, not yours.** The `router` preset decides how to route an LLM request (difficulty, domain=code, needs_tools). That's a different problem from whether this should interrupt you right now. A custom schema asking "worth interrupting?" still gets a generic urgency judgment from a model that knows nothing about you, your day, or who's messaging. Two more problems with the numbers you showed:
- `noul 0.229` is a score, not a probability, until you calibrate it against your own labels. Treat it as a ranking only.
- Your difficulty result came back with confidence 0.43. That's a coin-flip-quality signal driving whether the heavy model runs.

**Use winnow as a feature source, not the decision-maker.** Its real strength here is `choice` over a fixed list of 10–20 buckets that you define. That's a clean, inspectable input for the learning described in §3. Make the actual relay decision with rules plus per-bucket rates.

**Tier 2 ("heavy model when hard") is backwards for a walking user.** Your measured stage 1 is 203 words in 8.4s, and about 90% of that is thrown away. Flip it:
- **First card:** one fast call that writes straight to the screen budget.
- **Tap "more":** run the heavy model. Better, start it in the background while the first card is showing, so the expansion is instant.

This removes the difficulty router entirely. The user's tap becomes the "this is hard" signal, and it's free and correct.

**The simplest version that's still good:** hand-written rules for which incoming items may interrupt (source, sender, bucket), questions you ask always get answered, one fast model to compress, the heavy model only on tap, and a log of every decision. Add winnow later, only if replaying the log shows it beats the rules.

**Running cost:** a 7.5B decision model plus a fast generative model plus a heavy one, all on one RTX 4050, will fight over GPU memory. "Cheap" Tier 0 may cost you the fast tier's latency.

## 2. Hard parts you're underestimating

1. **Show-or-drop is the wrong choice.** You need now / at the next pause / batched digest / never. Most items should be deferred, not dropped. A binary gate turns every borderline item into a miss.
2. **The hardware already gives you an attention signal, and you're not using it.** `0x02`/`0x03` (head up/down) and `0x06`/`0x07` (worn/removed) are in your decode table. Head-up as "I'm looking now, show me the queue" is the best interruption mechanism the G1 has. Pulling the queue on head-up beats pushing interruptions for nearly everything.
3. **A silent drop is a failure you can't see.** You called it "the most important feature," but a silently missed important message destroys trust in a way a false alarm doesn't. You need a reviewable log of everything that was suppressed.
4. **No reaction is your most common outcome, and you don't record it.** Cards stay on screen until dismissed, and there's no timeout event. You only log reactions, so you can't see the base rate and every learned weight is biased.
5. **Rate limits and merging.** Hard cap on interruptions per hour, cooldowns, and collapse five messages from one thread into one card. None of this is in the design.
6. **Staleness and concurrency.** A card that's 10 seconds late can be worse than none. Also, a tap during an in-flight `more()` starts a second task that edits the same stack (`session.py:303`), and the expand gets credited to whichever card happened to be on top.
7. **Where does the Python run while you walk?** A laptop in a backpack? A Pi? A phone? bleak plus a local 7B model all day on battery is a hard platform question nobody has answered yet.
8. **Input.** Questions are typed at a `g1>` prompt. You can't do that while walking, and the code has no voice path (the G1 has mics).

## 3. The feedback loop

**Your code already mixes up the signals.** The comment at `memory.py:46` says expand means "topic matters, summary too thin." Then it credits the topic +0.5 and nothing goes to quality. Worse, **dismiss is also your "back" button.** After two expansions (clearly interested), popping back up penalises that topic at −0.6 per pop. The current scoring actively learns the wrong thing.

**How real systems separate relevance from quality:** they treat them as separate decisions with separate outcome signals.
- **Relevance** ("should this have been shown?") is judged at impression level: ignored / timed out, dismissed within ~2s, or engaged.
- **Quality** ("was this summary good?") is judged within the card: expand then fully read (wanted depth), expand then immediate back (the summary was bad or confusing), how long you looked before reacting.
- **Log every impression**, including no reaction. Otherwise you only learn from items you reacted to.

**The bigger trap: the gate learns only from what it lets through.** Dropped items never get labels, so the gate confirms its own choices and drifts towards showing less and less. Two fixes:
- Let a small fraction of below-threshold items through to test the threshold.
- Better for one user: **a 60-second nightly review** of what was shown and what was suppressed, with yes/no. For a single person, ~30 clean explicit labels a day beat any inference from taps. Your volume (tens of events a day) is far too small for implicit learning to converge in less than weeks.

**Keyword weighting is the wrong basis.** `extract_keywords` takes 12 frequent words from the full stage-1 answer, so each reaction credits words like "layers", "cells", "water". It's noise spread thin. And the weights only change the compression prompt's wording (they're also injected into stage 1, `pipeline.py:497`, which contradicts your brief). They never decide what gets relayed. The loop doesn't control the thing it's meant to learn.

**Use instead:** per-feature counts in SQLite over structured fields (source, sender, winnow bucket, time of day, head-up state), smoothed as Beta(engaged, not engaged). It's inspectable, a few lines of SQL, and you can explain every decision. I agree on no vector DB. If you later want "similar to things I starred," a few thousand embeddings stored as blobs in SQLite with numpy cosine similarity is plenty.

**Drop the LLM-written profile.** It can invent preferences and drift, and it's barely inspectable. If you keep it, make it a human-editable rules file the LLM only proposes changes to.

## 4. Toy vs genuinely useful

- **Toy:** a chat client whose screen sits on your face. That's what exists today, minus the face.
- **Useful requires three things, none built yet:**
  1. A real event stream that matters to you.
  2. All-day reliability: reconnects, battery, a host device you can carry.
  3. Measured trust: you never miss the important thing, and interruptions per hour stay low and justified.

Compression quality is a solved, minor problem by comparison. Your 8.4s+0.8s pipeline already does it.

## 5. What to cut, and the one thing to build first

**Cut:**
- Tier 3, the plugin registry and entry points. One user doesn't need them, and `resolve_config` changes a shared config while `run_query` uses one client for both stages, so per-role endpoints can't work anyway.
- The refiner.
- The Tier 2 difficulty routing (replace with the heavy model on tap).
- Keyword weights.
- Most provider presets.

**Keep:** the driver, the deterministic budget backstop (switched to lines), and SQLite (reshaped as below).

**Build first:** one end-to-end loop on the real glasses, fed by one real event source. Every candidate gets written to a `decisions` table: candidate, features, decision (shown / deferred / dropped), reason, and outcome (including timeout). The gate is hand-written rules, and there's a nightly review command to label the day. Run it for a week. Then replay winnow over the logged candidates against your labels. That one table settles the tiering question with evidence instead of architecture, and it's the training data for everything in §3.

I didn't change any files.
