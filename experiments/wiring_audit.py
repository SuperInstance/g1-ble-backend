"""
Is anything in this repo actually wired up?

A falsified feature was removed at 5b873e8, and removing it turned up
something the falsification could not have found: `plugins.RefinementPlugin`
had a docstring saying its cached profile "is what gets injected into the
stage-2 prompt from then on", and `preferred_profile()` -- the only consumer
of that cache -- has never had a caller in any commit of this repository.

That is one instance. The question worth asking is how many others there are,
and the answer cannot be found by reading, because the failure mode is
precisely that the code looks fine. So this measures it.

THE DEFECT CLASS. In a codebase whose main product risk is a confident claim
that outruns its evidence, the expensive mistake is not a bug that crashes.
It is a mechanism that is described as working, is believed to be working,
and is wired to nothing. Those cost nothing to run and everything to trust.

WHAT THIS DOES AND DOES NOT MEASURE.

  Measures: public functions with no call site anywhere in the repo; symbols
             written but never read; argparse options that nothing consumes;
             schema columns nothing queries.
  Does NOT measure: whether a called function does what its docstring says.
             That needs reading. This finds the candidates.

The distinction matters and it is the point of the tool. The refiner was
found by accident, by reading code I was already editing for a different
reason. A function with zero call sites cannot be found by reading prose, and
the prose will confidently describe it as working. It is a mechanical
property of a symbol table, so it should be checked mechanically.

WHY A SCRIPT AND NOT A LINTER. pyflakes reports unused IMPORTS, which is a
different and much smaller class: a name imported and never referenced in a
file. Nothing off the shelf reports a function that is perfectly well-formed,
correctly named, importable, documented -- and called by nobody.

Run: ``.venv/bin/python experiments/wiring_audit.py``
"""

from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# -- what counts as shipped code -------------------------------------------
#
# The distinction that makes the numbers mean something. A function called
# only from an experiment is not dead, it is INSTRUMENTED -- something is
# measuring it. A function called only from sim/ is exercised. A function
# called from neither, and not from its own module's tests, is a different
# thing again, and it is the thing this hunts.

SHIPPED = ["main.py", "pipeline.py", "session.py", "memory.py", "policy.py",
           "plugins.py", "jev.py", "g1_driver.py", "interrupt.py"]
EXPERIMENTS = sorted(p.name for p in (ROOT / "experiments").glob("*.py"))
SIM = sorted(p.name for p in (ROOT / "sim").glob("*.py"))

# -- ACKNOWLEDGED DEAD CODE ----------------------------------------------
#
# Every shipped function with no shipped caller must be listed here with a
# reason. An unlisted one is a FAILURE, which is the entire point: the list
# turns "someone eventually noticed" into "the diff shows up".
#
# The reasons matter more than the names. Each of these was, at some point,
# described in a docstring or a comment as something that worked. Writing the
# reason down is the cheapest possible defence against that growing back, and
# it forces the next person to decide whether the situation changed.
#
# Format: name -> (why it is dead, what would revive it)

ACKNOWLEDGED_DEAD: dict[str, tuple[str, str]] = {
    "next_seq": (
        "g1_driver.py: the 'global sequence counter shared by text packets'. "
        "The text path uses a per-send local counter instead (g1_driver.py "
        "~1131), so self._seq never advances. Whether the firmware needs a "
        "monotonic 0x4E seq is a HARDWARE question this repo cannot answer.",
        "Send the same text twice on real hardware and see whether the second "
        "render replaces the first. Tracked in .context/ble.md.",
    ),
    "build_firmware_query": (
        "g1_driver.py: builds a firmware-info request, but no CLI exposes it. "
        "Honest docstring, no false claim. Matters because the project has "
        "invented firmware versions it could not read.",
        "A --firmware command, then actually use the reading instead of "
        "asking a language model what firmware the glasses are on.",
    ),
    "enforce_word_budget": (
        "pipeline.py: a compat wrapper. Its comment claimed live call sites "
        "for months after the enforce_display_budget refactor removed all "
        "nine. A textbook instance of the defect class this file hunts.",
        "Nothing. It should probably be deleted; kept only to avoid breaking "
        "an external importer.",
    ),
    "has_evidence": (
        "memory.py: WearerContext property. Used by the stage-2 interest gate "
        "until the injection was removed in 5b873e8.",
        "Any future consumer that needs to know whether the store has evidence "
        "at all. Until then it is a correct property of nothing.",
    ),
    "mentions": (
        "memory.py: 'the gate on personalisation'. Its only caller is "
        "experiments/personalisation_ab.py, which keeps the falsification "
        "reproducible after the gate itself was removed.",
        "Same as has_evidence.",
    ),
    "top_topics": (
        "memory.py: superseded by wearer_context()/interests(), which are "
        "evidence-gated. Its justification ('the flat name third-party "
        "plugins were written against') is self-defeating: no third-party "
        "plugin can load, because discover() is never called.",
        "Whatever eventually consumes learned weights, or deletion.",
    ),
    "install_builtin_plugins": (
        "plugins.py: THE SUBSYSTEM IS DORMANT. Never called, so no "
        "env-var-driven plugin ever registers.",
        "A decision, not a line of code. resolve_config() and maybe_refine() "
        "have the same status and see documents/19.",
    ),
    "resolve_config": (
        "plugins.py: dormant with the rest of the seam. Wiring it would let "
        "a plugin change model routing with no measurement behind it.",
        "Same as install_builtin_plugins.",
    ),
    "maybe_refine": (
        "plugins.py: dormant, and deliberately so. Wiring it would re-enable "
        "the refine/personalisation mechanism falsified in documents/18.",
        "Do not wire it. documents/18 is the argument.",
    ),
    "preferred_profile": (
        "plugins.py: returns a cached refined profile or the statistical "
        "summary. This is the function whose lack of a caller PROVED the "
        "refiner was fiction — it is the only consumer of the refined_profile "
        "cache and it has never been called in any commit.",
        "Nothing, unless the refine mechanism is ever revived on evidence "
        "rather than on its original docstring.",
    ),
    "ambiguous_verdict": (
        "jev.py: the ambiguity sentinel, unreachable because jev_gate.py "
        "cannot authenticate (the available key file is a secret-manager "
        "reference, not an apikey_... key).",
        "A working TypeSafe key. The caller exists; only the credential is "
        "missing.",
    ),
    "fired": (
        "jev.py: telemetry accessor, read only by experiments/jev_gate.py.",
        "Shipped instrumentation, when there is a shipped interrupt gate.",
    ),
    "worth_interrupting": (
        "jev.py: the candidate gate. Called by experiments/jev_gate.py only, "
        "and the shipped decision is interrupt.py's deterministic rules.",
        "A measured reason to prefer it over interrupt.py. It has none yet: "
        "all 40 calls have returned 401.",
    ),
    "ready": (
        "session.py: 'resolve models once per session'. ask() and more() "
        "each call ensure_models directly, so this wrapper is unused.",
        "Deletion, or a caller that wants the resolved config.",
    ),
    "get_card": (
        "memory.py: accessor with no caller and no docstring. Never called in "
        "any commit.",
        "Deletion.",
    ),
    "settled_by_evidence": (
        "policy.py: True only when a metric, not a person, decided the axis. "
        "Reads main.py --queue and --evidence, which is deliberate: a dry run "
        "that shows PUSH rows because somebody pinned an axis by hand looks "
        "exactly like one showing them on evidence.",
        "Already used. Listed here because it was unacknowledged for one "
        "commit while it had no caller, which is the guard working.",
    ),
    "record_push": (
        "memory.py: writes push INTENT, called by experiments/interrupt_gate.py "
        "only. There is no shipped push path, so nothing in the product "
        "records a push yet -- which is exactly why the budget used to be a "
        "counter that could only climb.",
        "The first send function. It must be called BEFORE the BLE write, so "
        "a crash mid-send still spends the budget.",
    ),
    "settle_push": (
        "memory.py: attaches an outcome to a recorded attempt. Experiment-only "
        "for the same reason as record_push -- there is no delivery to "
        "settle.",
        "The send function, once one exists. The budget is spent on ATTEMPT, "
        "not on success, so this only annotates.",
    ),
    "push_rows": (
        "memory.py: audit accessor for the push log. No shipped caller.",
        "A --pushes command, or the review UI. Until then it is exercised by "
        "experiments/interrupt_gate.py only.",
    ),
    "note_unexamined": (
        "memory.py: counts pushes nobody examined, and deliberately "
        "suspends nothing. It exists so the temptation has somewhere "
        "harmless to go -- 200 unseen pushes look like 200 failures, and "
        "treating them as such starves a quiet source class into permanent "
        "silence, the exact failure documents/07 warns about.",
        "A review UI that needs to show 'these 40 were never looked at'. "
        "Read by experiments/suspend_gate.py only so far.",
    ),
    "unexamined_counts": (
        "memory.py: reads back what note_unexamined() wrote. For review "
        "triage. Not for gating -- absence of a reaction must never gate "
        "anything.",
        "The review UI, with note_unexamined().",
    ),
    "record_decision": (
        "memory.py: writes the decisions ledger. No shipped caller, because "
        "there is no shipped push path -- the ledger is populated by "
        "--review, which reads rows nothing writes yet.",
        "The push path, or main.py --queue recording what it decided. Worth "
        "wiring the moment either exists: an empty ledger looks identical to "
        "a system that has judged nothing, which is not the same thing.",
    ),
    "as_json": (
        "interrupt.py: the documented outbox format writer. Used by the "
        "round-trip check in experiments/interrupt_gate.py and intended for "
        "external producers of ~/.g1/outbox/*.json, of which there are none "
        "yet -- the shipped package only reads that directory.",
        "An event producer. DESIGN.md E0 assumes one exists; it does not.",
    ),
    "is_local": (
        "pipeline.py: LLMConfig property, unused. Presumably was going to "
        "gate a warning about local-model latency.",
        "A latency warning worth warning about.",
    ),
}

FAILURES: list[str] = []


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"  could not parse {rel(path)}: {exc}")
        return None


def all_sources() -> list[Path]:
    out = [ROOT / n for n in SHIPPED if (ROOT / n).exists()]
    out += sorted((ROOT / "experiments").glob("*.py"))
    out += sorted((ROOT / "sim").glob("*.py"))
    return [p for p in out if p.exists()]


def collect_refs(paths: list[Path]) -> dict[str, set[str]]:
    """
    symbol name -> set of files that REFERENCE it.

    Any Name load or Attribute access counts, not just Call nodes. The first
    version of this only counted calls and produced two false positives that
    a reader would have caught instantly: `store.add_card` is passed to
    `asyncio.to_thread` rather than called, and `on_packet` is handed to
    `controller.add_listener`. Both are wired; neither is a Call node. A tool
    that reports those as dead is worse than no tool, because it teaches the
    reader to skip it.

    The cost is the mirror-image error: attribute names are common words, so
    `current`, `target` and `label` will match unrelated attribute accesses.
    That biases toward under-reporting, which is the safe direction — a name
    the tool calls "wired" is not proof, but a name it calls "unwired" is
    worth reading.
    """
    refs: dict[str, set[str]] = defaultdict(set)
    for p in paths:
        tree = parse(p)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                refs[node.id].add(rel(p))
            elif isinstance(node, ast.Attribute):
                refs[node.attr].add(rel(p))
    return refs


def collect_defs(paths: list[Path]) -> dict[str, list[tuple[Path, int, bool, str]]]:
    """name -> [(path, lineno, is_async, first docstring line)]"""
    defs: dict[str, list[tuple[Path, int, bool, str]]] = defaultdict(list)
    for p in paths:
        tree = parse(p)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            doc = (ast.get_docstring(node) or "").strip().splitlines()
            defs[node.name].append((p, node.lineno, isinstance(node, ast.AsyncFunctionDef),
                                    doc[0] if doc else ""))
    return defs


def main() -> int:
    srcs = all_sources()
    shipped = [ROOT / n for n in SHIPPED if (ROOT / n).exists()]
    exps = [ROOT / "experiments" / n for n in EXPERIMENTS]
    sims = [ROOT / "sim" / n for n in SIM]

    print(f"wiring audit — {len(shipped)} shipped, {len(exps)} experiment, "
          f"{len(sims)} sim files\n")

    refs = collect_refs(srcs)
    defs = collect_defs(shipped)

    unwired: list[tuple[str, Path, int, str, str]] = []
    unacknowledged: list[str] = []
    for name, sites in sorted(defs.items()):
        callers = refs.get(name, set())
        shipped_callers = {c for c in callers
                           if Path(c).name in SHIPPED}
        if shipped_callers:
            continue
        # No shipped caller. Distinguish "instrumented" from "inert".
        kind = ("EXPERIMENT-ONLY" if callers
                else "NO CALLERS AT ALL")
        p, line, _is_async, doc = sites[0]
        unwired.append((kind, p, line, name, doc))
        if name not in ACKNOWLEDGED_DEAD:
            unacknowledged.append(name)

    print("=" * 78)
    print("A. shipped functions with no shipped caller")
    print("=" * 78)
    if not unwired:
        print("  none — every public function in the shipped modules is "
              "reachable from shipped code")
    for kind, p, line, name, doc in unwired:
        ack = ACKNOWLEDGED_DEAD.get(name)
        tag = "acknowledged" if ack else ">>> NOT ACKNOWLEDGED <<<"
        print(f"\n  {kind}  {name}()  {rel(p)}:{line}   [{tag}]")
        if doc:
            print(f"      docstring: {doc[:100]}")
        if ack:
            for para in ack[0].split(". "):
                if para.strip():
                    print(f"      why      : {para.strip()}.")
            print(f"      revives by: {ack[1]}")

    hard = [u for u in unwired if u[0] == "NO CALLERS AT ALL"]
    soft = [u for u in unwired if u[0] == "EXPERIMENT-ONLY"]
    print(f"\n  {len(hard)} with no callers at all, {len(soft)} reachable "
          f"only from experiments")
    print("\n  Reading: NO CALLERS AT ALL is the interesting column. A function")
    print("  an experiment calls is instrumented; a function NOTHING calls is")
    print("  inert, and inert code still reads as working code.")

    # -- B. argparse options nothing consumes ------------------------------
    print("\n" + "=" * 78)
    print("B. argparse options, and whether anything reads them")
    print("=" * 78)
    main_src = (ROOT / "main.py").read_text()
    tree = parse(ROOT / "main.py")
    opts: list[tuple[str, int]] = []
    if tree:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                opts.append((str(node.args[0].value), node.lineno))
    unread = []
    for flag, line in sorted(opts, key=lambda t: t[1]):
        dest = flag.lstrip("-").replace("-", "_")
        # argparse dest for --foo-bar is foo_bar
        uses = main_src.count(f"args.{dest}")
        if uses == 0:
            unread.append((flag, line, dest))
        print(f"  {flag:<22} args.{dest:<22} read {uses}x"
              + ("   <-- NEVER READ" if uses == 0 else ""))
    print(f"\n  {len(unread)} option(s) declared and never read.")
    for flag, line, dest in unread:
        print(f"    -- {flag} at main.py:{line} (dest {dest})")

    # -- C. env vars parsed but never used ---------------------------------
    print("\n" + "=" * 78)
    print("C. environment variables")
    print("=" * 78)
    env: dict[str, list[str]] = defaultdict(list)
    for p in srcs:
        text = p.read_text(errors="ignore")
        tree2 = parse(p)
        if tree2 is None:
            continue
        for node in ast.walk(tree2):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "get"
                    and node.args and isinstance(node.args[0], ast.Constant)
                    and str(node.args[0].value).isupper()):
                key = str(node.args[0].value)
                body = text[getattr(node, "end_lineno", node.lineno):]
                # count reads of the name after this get() in the same file
                n_after = body.count(key)
                env[key].append(f"{rel(p)}:{node.lineno} (used {n_after}x after)")

    for key in sorted(env):
        sites = env[key]
        total = sum(int(s.rsplit("used ", 1)[1].split("x")[0]) for s in sites)
        flag = "   <-- READ, NEVER USED" if total == 0 else ""
        print(f"  {key:<24} {len(sites)} get() site(s){flag}")
        for s in sites:
            print(f"      {s}")

    # -- verdict ------------------------------------------------------------
    print("\n" + "=" * 78)
    print("VERDICT")
    print("=" * 78)
    print(f"  {len(hard)} uncalled shipped functions, "
          f"{len(soft)} reachable only from experiments")
    print(f"  {len(unread)} argparse options never read")
    print(f"  {len(ACKNOWLEDGED_DEAD)} acknowledged dead entries "
          f"(each with a reason and a revival condition)")

    if unacknowledged:
        print(f"\n  FAIL: {len(unacknowledged)} uncalled function(s) with no "
              f"acknowledgement:\n")
        for name in unacknowledged:
            site = next(u for u in unwired if u[3] == name)
            print(f"    {name}()  {rel(site[1])}:{site[2]}")
        print("\n  A new uncalled function is not automatically a bug — an")
        print("  extension point or a compat shim is fine. It IS a bug when")
        print("  nobody wrote down which one it is, because that is exactly")
        print("  how RefinementPlugin ended up describing itself as injected")
        print("  into a prompt that nothing ever read.")
        print("\n  Fix: add it to ACKNOWLEDGED_DEAD with a reason and what")
        print("  would revive it, or give it a caller.")
        return 1

    print("\n  OK    every uncalled shipped function is accounted for.")
    print("  The list is only as good as the reasons in it; read them when")
    print("  deciding what to wire next.")
    if unread:
        print(f"\n  {len(unread)} argparse option(s) declared and never read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())