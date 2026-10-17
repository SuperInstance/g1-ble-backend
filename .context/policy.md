# Sector digest: `policy`

> Written by an independent reviewer reading the code, not by the
> agent that wrote it. Point-in-time: 2026-10-16.

## policy

Scope: `policy.py` (the axes, resolvers and overrides), its only runtime consumer `session.py`, the evidence source `sim/ledger.py:229`, the command-line options at `main.py:426-458`, and the two design docs (13 is Claude's, 14 is zcode's).

### Traps
1. **Saved overrides are ignored at runtime.** `main.py:237` creates `Session` without passing a policy, so `session.py:117` calls `resolve()` instead of `load()`. Running `--set-policy detail_generation=on` saves the override and `--policy` shows it, but tapping for more detail stays switched off.
2. **Nothing on real hardware supplies evidence.** The only evidence source is `evidence()` at `sim/ledger.py:229`, which runs only in the simulator and never supplies `rho`. `main.py:454` calls `load()` with no evidence, so on hardware every axis falls back to its default and shows "??".
3. **The drill-miss rate is rigged by the default.** While detail is off, every tap records a `drill_miss` (`session.py:222`). A rate counted from those events is therefore 100%, which passes the 0.30 threshold at `policy.py:217` automatically. The simulator defines the rate differently (`outcome='unobservable'` on rows with `depth>0`, `sim/ledger.py:219`). That is two stores with two definitions.

### State
- Five axes are declared at `policy.py:79-149`, each with options, a conservative default, a named deciding metric and a resolver (`policy.py:169-241`). `__post_init__` (`policy.py:71`) checks that each default is one of its options.
- Only `detail_generation` is read at runtime (`session.py:221`). Nothing reads `examination_source`, `g1_model`, `interrupt_gate` or `push_enabled`. Head-up (`0x02`) isn't bound to any action (`session.py:68`), and there is no push path or noise filter in the code.
- Overrides are saved to `~/.local/share/g1-ble-backend/policy.json` (`policy.py:326`). An override is marked as having enough evidence (`policy.py:314`), so it no longer shows up in `unresolved()`.
- **Measured**, offline, on small self-labelled sets (n=40 and n=50, doc 14 line 3):
  - AUC 0.898 for noise vs queue-worthy, 0.958 for noise vs interrupt, 0.813 for glance vs interrupt.
  - The noise model takes 5826 of 6141 MiB of VRAM.
  - The ρ = 1.6 cliff comes from a simulation (Turn C). Doc 13 §6 calls that simulation nearly circular, because the sine-wave gait caps the result by construction.
- **Derived:** 299 clean pushes is ln 0.05 / ln 0.99. It only holds if there are zero false pushes (`policy.py:46-51`).
- **Assumed, with no data behind them:** queue worth ≥ 0.5 (`:191`), drill-miss rate ≥ 0.30 (`:217`), 14 label-days (`:227`), and the 100 and 220 label minimums (`:55-56`).

### Open
- **examination_source.** Settled by ρ = θ/A_b from the day-1 ten-minute walk without looking up. Doc 13 §3 uses a different test instead: if p99 of the up→down durations is over 333 ms, only the TouchBar opens the queue. One metric needs to be chosen. Doc 13 also argues that dwell time can't tell looking at the glasses from looking at a sign, so even "both" only makes `examined` an upper bound.
- **g1_model.** The resolver turns the model on when `queue_worth_rate ≥ 0.5`. In doc 14 that number is the milestone-1 "continue" test (was the queue worth glancing at), not a measure of what the filter adds. The metric that would settle it: review labels on suppressed items compared with labels on shown items. The VRAM cost isn't counted anywhere.
- **interrupt_gate.** `clean_pushes` counts `label=1 AND already_knew=0` (`sim/ledger.py:239`) and never subtracts false pushes, although the 299 bound requires zero. Doc 13 §2 says a learned gate "can never be shown" to be good enough, yet the code switches to it at 299. Also, there are no pushes while `push_enabled` is off, so this axis can't gather evidence until that one flips, and the code doesn't model that dependency. Finally, `enough_evidence = clean > 0` (`:210`) means a single push removes the "??" marker.
- **push_enabled.** This one is genuinely contested, so the provenance at `policy.py:147` ("both convergence rounds") is wrong:
  - Doc 13 §5 defines the milestone as rule-based pushes (meeting 10 minutes ahead, blocked job, battery).
  - Doc 14 §4–5 allows no pushes in milestone 1.
  - The code's default follows doc 14.
  - `label_days` counts any distinct days that have a label, not a continuous fortnight of tier labels (`sim/ledger.py:244`).
- **detail_generation.** Needs one agreed definition of the drill-miss rate (Trap 3) before day 14.

### Verify before changing
```
python3 experiments/policy_axes.py        # resolver scenarios plus override/invalid-option checks (I could not run this)
python3 main.py --policy-axes             # list the declared axes
python3 main.py --policy                  # expect every axis to show ?? on real hardware
grep -n "policy" main.py session.py       # Trap 1: Session still gets no policy
grep -rn "evidence()" --include=*.py .    # Trap 2: the simulator is the only evidence source
grep -n "drill_miss" session.py memory.py sim/ledger.py   # Trap 3: the two definitions
```

### Falsifiers
- **Head-up as a signal:** a real walk with ρ < 1.6 (or p99 > 333 ms) means head-up never counts as examining a card. Fabricated glances on a real walk with ρ ≥ 1.6 would mean the cliff was a simulation artefact and "both" is unsafe.
- **Noise model:** if suppressed items get review labels at least as good as shown ones, the model adds nothing, whatever `queue_worth_rate` says.
- **Learned interrupt gate:** any false push in the first ~299 breaks the zero-failure assumption, so `learned` must not be reachable by counting clean pushes alone.
- **Pushes off by default:** doc 13's continue rule is earned ≥ 15, false pushes ≤ 1 and redundant < earned over 14 days. If that passes with pushes on, keeping them off cost a milestone. If redundant ≥ earned, pushes are pointless anyway.
- **Detail on tap:** a drill-miss rate below 0.30 under a fair definition, while people still give up on taps, means detail isn't the bottleneck.
- **The whole approach:** if no live code ever calls `resolve()` with real evidence, "settle it at setup" is just hard-coded defaults plus a command-line switch.
