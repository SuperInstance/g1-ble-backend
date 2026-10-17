"""
plugins.py
==========

The extension seam.

Everything beyond the core pipeline — a fast local model for rapid
summarisation, an external service like typesafe.ai or laya, a different
provider for the meta pass — plugs in here rather than being hardcoded.

The contract is deliberately tiny. A plugin declares which **roles** it can
serve and optionally:

* returns a modified config for a role (to swap model or provider), and/or
* supplies a ``refine`` pass that turns accumulated reaction history into a
    sharper preference profile once there is enough of it to be worth a
    slower, more capable model.

Two ways in:

1. **In-process** — call :func:`register` before starting the session.
2. **Out-of-process** — declare a setuptools entry point in the group
   ``g1_ble_backend.plugins``; :func:`discover` picks it up automatically::

       # pyproject.toml of your package
       [project.entry-points."g1_ble_backend.plugins"]
       my-plugin = "my_package.g1:MyPlugin"

A worked example::

    from plugins import ModelRolePlugin, register

    # tev1 is a small local model: good enough to compress fast.
    register(ModelRolePlugin("tev1-fast", model="tev1", roles=("stage2",)))

That is the whole integration. Nothing else in the codebase changes.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional, Protocol

from memory import FeedbackStore
from pipeline import LLMConfig

log = logging.getLogger("g1.plugins")

ENTRY_POINT_GROUP = "g1_ble_backend.plugins"

# Roles a plugin may claim. Keeping this explicit means a typo in a plugin is
# caught immediately rather than silently never being called.
VALID_ROLES = ("stage1", "stage2", "refine", "relay")

# How much history is worth spending a slower, more capable model on.
DEFAULT_REFINE_THRESHOLD = 20


class Plugin(Protocol):
    """What a plugin must provide."""

    name: str
    roles: tuple[str, ...]

    def stage_config(self, role: str, config: LLMConfig) -> LLMConfig:
        """Return the config to use for ``role``. Default: unchanged."""
        ...

    async def refine(self, store: FeedbackStore, config: LLMConfig) -> Optional[str]:
        """Optional: synthesise a preference profile from reaction history."""
        ...


# ---------------------------------------------------------------------------
# Built-in plugin implementations
# ---------------------------------------------------------------------------

@dataclass
class ModelRolePlugin:
    """
    Pin a specific model to one or more roles.

    The common case: a fast local model for stage 2, a larger one for stage 1.
    Optionally binds a different provider too, which is how an external
    service (typesafe.ai, laya, or any OpenAI-compatible endpoint) slots in.
    """

    name: str
    model: Optional[str] = None
    roles: tuple[str, ...] = ()
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    provider: Optional[str] = None
    priority: int = 0

    def stage_config(self, role: str, config: LLMConfig) -> LLMConfig:
        if role not in self.roles:
            return config
        if self.provider:
            config.provider = self.provider
        if self.base_url:
            config.base_url = self.base_url
        if self.api_key:
            config.api_key = self.api_key
        if self.model:
            if role == "stage1":
                config.primary_model = self.model
            elif role == "stage2":
                config.compressor_model = self.model
            else:
                # refine/relay fall back to the compressor slot for transport.
                config.compressor_model = self.model
        return config


REFINER_SYSTEM_PROMPT = (
    "You turn a person's reaction history into durable summarisation "
    "preferences for a pair of smart glasses.\n\n"
    "You are given cards that were shown and how the wearer reacted: "
    "'expand' means they asked for more, 'confirm' means it was good enough, "
    "'star' means it mattered, 'dismiss' means it did not land.\n\n"
    "Write 3 short bullet points, each one sentence, that would help a "
    "compressor choose what to put on a 25-word display for this specific "
    "person. State what to emphasise, what to cut, and how much detail they "
    "tend to want. Refer to concrete subjects from their history where useful.\n\n"
    "No preamble. No meta-commentary. Bullets only."
)


@dataclass
class RefinementPlugin:
    """
    The higher-level pass.

    Below a threshold this does nothing — there is no point asking a capable
    model to generalise from three events. Once enough reactions accumulate it
    synthesises a profile from the cards and their reactions and caches it.

    **NOTHING INJECTS THAT PROFILE ANYWHERE.** This docstring used to say the
    cached text "is what gets injected into the stage-2 prompt from then on",
    and that sentence was false for the entire life of the code:
    ``preferred_profile()``, the only consumer of the cache, has never had a
    caller in any commit of this repository. So this is a second
    personalisation mechanism that was written, described as live, and never
    connected to anything — while the stage-2 prompt it claimed to shape was
    being shaped by a different and now-removed block.

    It is kept because it is a plugin extension point reachable through the
    ``g1_ble_backend.plugins`` entry-point group, not because it works. If you
    are here to add personalisation back, read documents/18 first: the
    injection it was written for has been measured and removed, and the
    recommendation there is to leave it out.
    """

    name: str = "meta-refiner"
    roles: tuple[str, ...] = ("refine",)
    threshold: int = DEFAULT_REFINE_THRESHOLD
    model: Optional[str] = None
    priority: int = 10

    def stage_config(self, role: str, config: LLMConfig) -> LLMConfig:
        if role in self.roles and self.model:
            config.compressor_model = self.model
        return config

    @staticmethod
    def should_run(store: FeedbackStore, threshold: int = DEFAULT_REFINE_THRESHOLD) -> bool:
        """Enough history, and no fresh profile already computed."""
        try:
            if store.event_total() < threshold:
                return False
            cached = store.get_meta("refined_profile")
            last = store.get_meta("refined_at")
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("refine gate failed: %s", exc)
            return False
        if not cached:
            return True
        # Recompute once the store has grown meaningfully since the last pass.
        return store.event_total() - int(last or 0) >= max(threshold // 2, 5)

    async def refine(self, store: FeedbackStore, config: LLMConfig) -> Optional[str]:
        from pipeline import LLMStageError, _chat, make_client

        if not self.should_run(store, self.threshold):
            return None

        cards = store.recent_cards(limit=40)
        if not cards:
            return None

        lines = []
        for card in cards:
            reactions = ",".join(e.kind for e in store.events_for_card(card.id)) or "none"
            lines.append(f"- showed: {card.summary!r}\n  question: {card.query!r}\n  reactions: {reactions}")

        try:
            client = make_client(config)
            profile = await _chat(
                client,
                model=self.model or config.primary_model or config.compressor_model or "",
                system=REFINER_SYSTEM_PROMPT,
                user="Reaction history:\n\n" + "\n".join(lines),
                max_tokens=300,
                temperature=0.3,
            )
        except (LLMStageError, Exception) as exc:  # noqa: B014 - never fatal
            log.warning("refinement pass failed: %s", exc)
            return None

        profile = (profile or "").strip()
        if not profile:
            return None

        store.set_meta("refined_profile", profile)
        store.set_meta("refined_at", str(store.event_total()))
        log.info("refined preference profile updated from %d reactions",
                 store.event_total())
        return profile


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: list[Plugin] = []


def register(plugin: Plugin) -> Plugin:
    """Add a plugin. Later registrations with higher priority take precedence."""
    bad = [r for r in plugin.roles if r not in VALID_ROLES]
    if bad:
        raise ValueError(
            f"plugin {plugin.name!r} claims unknown role(s) {bad}; "
            f"valid roles are {list(VALID_ROLES)}"
        )
    _REGISTRY.append(plugin)
    log.debug("registered plugin %s for roles %s", plugin.name, plugin.roles)
    return plugin


def plugins() -> list[Plugin]:
    """Registered plugins, highest priority first."""
    return sorted(_REGISTRY, key=lambda p: getattr(p, "priority", 0), reverse=True)


def for_role(role: str) -> list[Plugin]:
    """Plugins claiming ``role``, highest priority first."""
    return [p for p in plugins() if role in getattr(p, "roles", ())]


def resolve_config(role: str, config: LLMConfig) -> LLMConfig:
    """
    Apply the highest-priority plugin for ``role``, if any.

    Each plugin is handed the config produced by the previous one, so several
    can compose (e.g. one swaps the provider, another swaps the model).
    """
    for plugin in for_role(role):
        try:
            config = plugin.stage_config(role, config)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("plugin %s failed to configure %s: %s",
                        plugin.name, role, exc)
    return config


def preferred_profile(store: FeedbackStore) -> str:
    """
    Best available preference block: a cached refined profile if one exists,
    otherwise the cheap statistical summary.

    Dead in practice: no caller anywhere in the repository or its history, so
    the "the two layers compose naturally" claim in an earlier version of this
    docstring described a composition that never existed. Both layers produce
    exactly the kind of prompt fragment that documents/18 measured and
    removed. Kept as an extension-point helper for third-party plugins, and
    deliberately NOT wired to any prompt.
    """
    try:
        cached = store.get_meta("refined_profile")
    except Exception:  # pragma: no cover - defensive
        cached = None
    if cached:
        return cached
    try:
        return store.preferences()
    except Exception:  # pragma: no cover - defensive
        return ""


def discover() -> int:
    """
    Load plugins declared via the ``g1_ble_backend.plugins`` entry point group.

    Returns the number of plugins loaded. Safe to call more than once; already
    registered plugin names are skipped.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover - Python < 3.8
        return 0

    loaded = 0
    try:
        found = entry_points(group=ENTRY_POINT_GROUP)
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("entry point discovery failed: %s", exc)
        return 0

    existing = {getattr(p, "name", None) for p in _REGISTRY}
    for entry in found:
        if entry.name in existing:
            continue
        try:
            factory = entry.load()
            candidate = factory() if callable(factory) else factory
            register(candidate)
            log.info("loaded plugin %s from entry point", entry.name)
            loaded += 1
        except Exception as exc:
            log.warning("could not load plugin %s: %s", entry.name, exc)
    return loaded


def install_builtin_plugins(config: LLMConfig | None = None) -> None:
    """
    Register the plugins implied by environment variables.

    ``G1_FAST_MODEL``      binds a fast model to stage 2 and relay work.
    ``G1_REFINE_MODEL``    model used by the higher-level pass.
    ``G1_REFINE_THRESHOLD`` reactions required before refining (default 20).

    Nothing is registered unless the corresponding variable is set, so the
    default install has exactly one plugin: the refiner.
    """
    if RefinementPlugin not in [type(p) for p in _REGISTRY]:
        threshold = int(os.environ.get("G1_REFINE_THRESHOLD", DEFAULT_REFINE_THRESHOLD) or DEFAULT_REFINE_THRESHOLD)
        register(RefinementPlugin(
            threshold=threshold,
            model=os.environ.get("G1_REFINE_MODEL"),
        ))

    fast = os.environ.get("G1_FAST_MODEL")
    if fast:
        register(ModelRolePlugin(
            name=f"fast:{fast}",
            model=fast,
            roles=("stage2", "relay"),
            base_url=os.environ.get("G1_FAST_BASE_URL"),
            api_key=os.environ.get("G1_FAST_API_KEY"),
            priority=5,
        ))

    # An entirely separate provider for the fast path, e.g. typesafe.ai or laya.
    external = os.environ.get("G1_EXTERNAL_BASE_URL")
    if external:
        register(ModelRolePlugin(
            name="external",
            roles=("stage2", "relay"),
            base_url=external,
            api_key=os.environ.get("G1_EXTERNAL_API_KEY"),
            provider=os.environ.get("G1_EXTERNAL_PROVIDER"),
            priority=1,
        ))


async def maybe_refine(store: FeedbackStore, config: LLMConfig) -> Optional[str]:
    """Run the highest-priority refiner if one is registered and the gate is open."""
    for plugin in for_role("refine"):
        refine = getattr(plugin, "refine", None)
        if refine is None:
            continue
        try:
            return await refine(store, config)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("plugin %s refine failed: %s", plugin.name, exc)
    return None