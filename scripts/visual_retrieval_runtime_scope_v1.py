from __future__ import annotations

"""Canonical runtime scope for Visual Retrieval & Adjudication V1.

The V1 installer composes production hooks once, but direct unit/diagnostic callers must
continue to observe the historical Engine/V2 surfaces unless they explicitly enter the
canonical production run. This module makes that boundary explicit instead of relying on
process lifetime or test ordering.

Only ``orchestrator.produce()`` activates V1 transport/reranking semantics. The Vision
contract fingerprint remains globally V1-bound because durable production audit identity
must reflect the installed production policy even before a run begins.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
import isco_video_agent.visual_selection as visual_selection
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import vision_stage_contract_v2 as contract
from scripts import visual_retrieval_adjudication_v1 as v1


_ACTIVE: ContextVar[bool] = ContextVar("isco_visual_retrieval_v1_active", default=False)
_INSTALLED = False


def active() -> bool:
    return bool(_ACTIVE.get())


@contextmanager
def visual_retrieval_runtime_scope():
    if _ACTIVE.get():
        yield
        return
    token = _ACTIVE.set(True)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _conditional_function(current, *, original_attr: str, marker: str):
    if getattr(current, marker, False):
        return current
    original = getattr(current, original_attr, None)
    if not callable(original):
        return current

    @wraps(current)
    def wrapped(*args, **kwargs):
        target = current if _ACTIVE.get() else original
        return target(*args, **kwargs)

    setattr(wrapped, marker, True)
    setattr(wrapped, "_isco_visual_scope_active_target", current)
    setattr(wrapped, "_isco_visual_scope_inactive_target", original)
    return wrapped


def _install_contact_sheet_scope() -> None:
    current_sampler = contract.legacy._sample_preview_frames
    scoped_sampler = _conditional_function(
        current_sampler,
        original_attr="_isco_contact_sheet_original",
        marker="_isco_contact_sheet_runtime_scope_v1",
    )
    contract.legacy._sample_preview_frames = scoped_sampler

    current_prompt = contract.legacy._visual_prompt
    scoped_prompt = _conditional_function(
        current_prompt,
        original_attr="_isco_contact_sheet_prompt_original",
        marker="_isco_contact_sheet_prompt_runtime_scope_v1",
    )
    contract.legacy._visual_prompt = scoped_prompt


def _install_health_scope() -> None:
    current = health.publish_provider_unavailable
    if getattr(current, "_isco_visual_capacity_runtime_scope_v1", False):
        return
    original = getattr(current, "_isco_visual_capacity_original", None)
    if not callable(original):
        return

    @wraps(current)
    def wrapped(provider: str, *, model: str = "*", quota_domain: str = "*", reason: str, source: str):
        target = current if _ACTIVE.get() else original
        return target(
            provider,
            model=model,
            quota_domain=quota_domain,
            reason=reason,
            source=source,
        )

    wrapped._isco_visual_capacity_runtime_scope_v1 = True
    wrapped._isco_visual_scope_active_target = current
    wrapped._isco_visual_scope_inactive_target = original
    health.publish_provider_unavailable = wrapped


class _ScopedRequestsProxy:
    def __init__(self, active_proxy):
        self._active_proxy = active_proxy
        self._base = getattr(active_proxy, "_base", active_proxy)

    def __getattr__(self, name):
        target = self._active_proxy if _ACTIVE.get() else self._base
        return getattr(target, name)

    def get(self, *args, **kwargs):
        target = self._active_proxy if _ACTIVE.get() else self._base
        return target.get(*args, **kwargs)

    def post(self, *args, **kwargs):
        target = self._active_proxy if _ACTIVE.get() else self._base
        return target.post(*args, **kwargs)


def _install_requests_scope() -> None:
    current = run181.requests
    if isinstance(current, _ScopedRequestsProxy):
        return
    if not isinstance(current, v1._Run181RequestsProxy):
        return
    run181.requests = _ScopedRequestsProxy(current)


def _install_search_scope() -> None:
    orchestrator.pexels_search_videos = _conditional_function(
        orchestrator.pexels_search_videos,
        original_attr="_isco_visual_retrieval_original",
        marker="_isco_visual_search_runtime_scope_v1",
    )
    orchestrator.pixabay_provider.search_videos = _conditional_function(
        orchestrator.pixabay_provider.search_videos,
        original_attr="_isco_visual_retrieval_original",
        marker="_isco_visual_search_runtime_scope_v1",
    )


def _install_selector_scope() -> None:
    current_opening = opening_director.select_opening_sequence
    scoped_opening = _conditional_function(
        current_opening,
        original_attr="_isco_visual_intent_original",
        marker="_isco_visual_selector_runtime_scope_v1",
    )
    opening_director.select_opening_sequence = scoped_opening
    if orchestrator.select_opening_sequence is current_opening:
        orchestrator.select_opening_sequence = scoped_opening

    current_section = section_visual_sequence.select_section_sequence
    scoped_section = _conditional_function(
        current_section,
        original_attr="_isco_visual_intent_original",
        marker="_isco_visual_selector_runtime_scope_v1",
    )
    section_visual_sequence.select_section_sequence = scoped_section
    if orchestrator.select_section_sequence is current_section:
        orchestrator.select_section_sequence = scoped_section

    current_single = visual_selection.select_with_recovery
    scoped_single = _conditional_function(
        current_single,
        original_attr="_isco_visual_intent_original",
        marker="_isco_visual_selector_runtime_scope_v1",
    )
    visual_selection.select_with_recovery = scoped_single
    if orchestrator.select_with_recovery is current_single:
        orchestrator.select_with_recovery = scoped_single


def _install_rank_scope() -> None:
    current = visual_selection.rank_and_interleave
    scoped = _conditional_function(
        current,
        original_attr="_isco_visual_rank_original",
        marker="_isco_visual_rank_runtime_scope_v1",
    )
    visual_selection.rank_and_interleave = scoped
    # opening_director and section_visual_sequence imported this function directly.
    if opening_director.rank_and_interleave is current:
        opening_director.rank_and_interleave = scoped
    if section_visual_sequence.rank_and_interleave is current:
        section_visual_sequence.rank_and_interleave = scoped


def _install_produce_scope() -> None:
    current = orchestrator.produce
    if getattr(current, "_isco_visual_retrieval_runtime_scope_v1", False):
        return

    @wraps(current)
    def wrapped(*args, **kwargs):
        with visual_retrieval_runtime_scope():
            return current(*args, **kwargs)

    wrapped._isco_visual_retrieval_runtime_scope_v1 = True
    wrapped._isco_visual_retrieval_runtime_original = current
    orchestrator.produce = wrapped


def install_visual_retrieval_runtime_scope_v1() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    # Scope the hooks that change observable legacy behavior. Pure metadata annotation
    # and the durable fingerprint remain installed globally by V1 itself.
    _install_contact_sheet_scope()
    _install_health_scope()
    _install_requests_scope()
    _install_search_scope()
    _install_selector_scope()
    _install_rank_scope()
    _install_produce_scope()

    # Run185 is the final semantic-adjudication layer. Install it only after all
    # V1/Run183 route and runtime-scope composition is complete so Gemini/Groq/OpenRouter
    # receive one identical semantic goal while direct historical diagnostics remain
    # unchanged outside canonical Production.
    from scripts.run185_visual_intent_adjudication import install_run185_visual_intent_adjudication

    install_run185_visual_intent_adjudication()
    _INSTALLED = True
    print(
        "Visual Retrieval V1 runtime scope installed: active only inside orchestrator.produce; "
        "direct V2/Run181 diagnostics preserve historical transport and health semantics; "
        "Run185 semantic adjudication composed last"
    )
