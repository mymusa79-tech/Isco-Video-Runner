from __future__ import annotations

"""Visual Retrieval & Adjudication Contract V1.

This layer closes the Run #182 architecture gap between stock retrieval and expensive
multimodal adjudication. It is deliberately advisory before Vision and fail-closed at
Vision itself:

1. Preserve stock-provider semantic metadata that the pinned Engine historically drops.
2. Build a deterministic local VisualIntent contract and rerank each provider pool before
   any cloud Vision review; the existing Engine duration/orientation ranking and all
   Security/semantic gates remain authoritative.
3. Diversify top candidates with a bounded MMR-style lexical penalty so near-duplicates
   do not consume the scarce review budget first.
4. Replace the fallback transport's three separate images with one six-tile chronological
   contact sheet. Groq documents each input image as 2048 tokens; one contact sheet keeps
   temporal evidence while reducing image-token pressure from 6144 to 2048.
5. Forward every Groq Vision response to the shared persisted model-capacity owner. A
   short TPM cooldown is waitable across production, Gold, and sibling subprocesses;
   daily/auth/model failures retain the existing hard-unavailable semantics.

No visual threshold, Cultural/Islamic gate, Security gate, candidate/review ceiling, or
semantic BLOCK behavior is weakened here.
"""

import hashlib
import json
import math
import re
import subprocess
import tempfile
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import requests as _requests

import isco_video_agent.opening_director as opening_director
import isco_video_agent.orchestrator as orchestrator
import isco_video_agent.section_visual_sequence as section_visual_sequence
import isco_video_agent.visual_selection as visual_selection
from scripts import provider_capacity_hardening as shared_capacity
from scripts import provider_health_registry as health
from scripts import run181_vision_mesh_closure as run181
from scripts import vision_stage_contract_v2 as contract
from scripts.provider_wire_attempt_contract import NoWireVisionStageError


CONTRACT_ID = "visual-retrieval-adjudication-v1"
CONTRACT_VERSION = 1
CONTACT_SHEET_FRAMES = 6
CONTACT_SHEET_COLUMNS = 3
CONTACT_SHEET_ROWS = 2
CONTACT_SHEET_MAX_BYTES = 2 * 1024 * 1024
CONTACT_SHEET_TIMEOUT_SECONDS = 35
GROQ_IMAGE_TOKENS = 2048
GROQ_FREE_TPM_HINT = 8000
GROQ_MAX_BOUNDED_WAIT_SECONDS = 65.0
GROQ_OUTPUT_TOKEN_RESERVE = 900

_INSTALLED = False
_INTENT: ContextVar["VisualIntent | None"] = ContextVar("isco_visual_intent_v1", default=None)

_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "with", "from",
    "for", "by", "as", "is", "are", "be", "being", "been", "that", "this", "these",
    "those", "person", "people", "someone", "something", "looking", "look", "looks",
    "standing", "sitting", "walking", "showing", "scene", "shot", "video", "footage",
}

# Small deterministic concept families cover recurring channel/editorial abstractions.
# They do not decide PASS/BLOCK; they only improve which already-returned stock result is
# reviewed first. Final multimodal adjudication remains mandatory.
_CONCEPTS = (
    frozenset({"boundary", "boundaries", "limit", "limits", "line", "space", "distance", "separate", "separation", "door"}),
    frozenset({"decision", "decide", "choice", "choose", "crossroad", "direction", "path", "turn"}),
    frozenset({"pressure", "stress", "overwhelm", "overwhelmed", "tension", "burden", "weight"}),
    frozenset({"discipline", "routine", "habit", "schedule", "calendar", "practice", "training"}),
    frozenset({"progress", "growth", "improve", "improvement", "climb", "stairs", "journey", "forward"}),
    frozenset({"comparison", "compare", "competition", "race", "mirror", "scale"}),
    frozenset({"focus", "attention", "concentration", "desk", "notebook", "work", "study"}),
    frozenset({"relationship", "relationships", "conversation", "talk", "friends", "family", "partner", "together"}),
    frozenset({"guilt", "hesitation", "hesitant", "conflict", "uncertain", "thoughtful", "pause"}),
    frozenset({"calm", "relief", "release", "peace", "quiet", "resolved", "clarity"}),
    frozenset({"isolation", "alone", "lonely", "distance", "empty", "solitude"}),
    frozenset({"restart", "recover", "recovery", "rise", "stand", "return", "begin", "start"}),
)


@dataclass(frozen=True, slots=True)
class VisualIntent:
    raw: str
    anchors: frozenset[str]
    expanded: frozenset[str]


def _stem(token: str) -> str:
    value = token.lower().strip("-_ ")
    if len(value) > 6 and value.endswith("ing"):
        value = value[:-3]
    elif len(value) > 5 and value.endswith("ed"):
        value = value[:-2]
    elif len(value) > 4 and value.endswith("es"):
        value = value[:-2]
    elif len(value) > 3 and value.endswith("s"):
        value = value[:-1]
    return value


def _tokens(text: object) -> set[str]:
    raw = re.findall(r"[a-z0-9]+", str(text or "").casefold())
    return {_stem(item) for item in raw if item not in _STOPWORDS and len(item) > 1}


def build_visual_intent(text: object) -> VisualIntent:
    raw = str(text or "").strip()
    anchors = _tokens(raw)
    expanded = set(anchors)
    for family in _CONCEPTS:
        normalized = {_stem(item) for item in family}
        if anchors & normalized:
            expanded.update(normalized)
    return VisualIntent(raw=raw, anchors=frozenset(anchors), expanded=frozenset(expanded))


def _slug_text(url: object) -> str:
    try:
        path = urlparse(str(url or "")).path
    except Exception:
        return ""
    parts = [part for part in path.split("/") if part]
    if not parts:
        return ""
    slug = max(parts, key=len)
    slug = re.sub(r"-?\d+$", "", slug)
    return slug.replace("-", " ").replace("_", " ")


def _engagement_score(meta: dict) -> float:
    values = []
    for key in ("views", "downloads", "likes"):
        try:
            value = max(0.0, float(meta.get(key) or 0))
        except (TypeError, ValueError):
            value = 0.0
        if value:
            values.append(math.log1p(value))
    if not values:
        return 0.0
    return min(1.0, sum(values) / (len(values) * 16.0))


def _annotate_candidate(provider: str, candidate: dict) -> dict:
    meta = candidate.get("_isco_visual_intelligence")
    if not isinstance(meta, dict):
        meta = {}
    semantic_text = " ".join(
        part for part in (
            str(meta.get("tags") or ""),
            str(meta.get("title") or ""),
            _slug_text(candidate.get("url")),
            str((candidate.get("user") or {}).get("name") or "") if isinstance(candidate.get("user"), dict) else "",
        ) if part.strip()
    )
    meta["provider"] = str(provider).strip().lower()
    meta["semantic_text"] = semantic_text[:1200]
    meta["semantic_tokens"] = sorted(_tokens(semantic_text))
    candidate["_isco_visual_intelligence"] = meta
    return candidate


def _candidate_tokens(provider: str, candidate: dict) -> set[str]:
    _annotate_candidate(provider, candidate)
    meta = candidate.get("_isco_visual_intelligence") or {}
    return set(meta.get("semantic_tokens") or ())


def _candidate_relevance(provider: str, candidate: dict, intent: VisualIntent, original_rank: int, total: int) -> float:
    cand = _candidate_tokens(provider, candidate)
    rank_prior = 1.0 - (float(original_rank) / max(1.0, float(total)))
    if not cand or not intent.expanded:
        semantic = 0.0
    else:
        exact = len(cand & set(intent.anchors))
        expanded = len(cand & set(intent.expanded))
        precision = expanded / max(1, len(cand))
        recall = expanded / max(1, len(intent.expanded))
        semantic = min(1.0, 0.55 * recall + 0.25 * precision + 0.20 * min(1.0, exact / max(1, len(intent.anchors))))
    meta = candidate.get("_isco_visual_intelligence") or {}
    engagement = _engagement_score(meta)
    # Local semantics dominate only when metadata exists. Provider search order remains a
    # meaningful prior and the Engine still applies duration/orientation/resolution next.
    return 0.62 * semantic + 0.33 * rank_prior + 0.05 * engagement


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def rerank_provider_candidates(provider: str, candidates: list[dict], intent: VisualIntent) -> list[dict]:
    if len(candidates) < 2 or not intent.raw:
        return list(candidates)
    scored = [
        (_candidate_relevance(provider, candidate, intent, index, len(candidates)), index, candidate)
        for index, candidate in enumerate(candidates)
    ]
    remaining = sorted(scored, key=lambda item: (-item[0], item[1]))
    selected: list[tuple[float, int, dict]] = []
    selected_tokens: list[set[str]] = []
    while remaining:
        best_pos = 0
        best_value = -10.0
        for pos, item in enumerate(remaining):
            base, original_rank, candidate = item
            tokens = _candidate_tokens(provider, candidate)
            similarity = max((_jaccard(tokens, prior) for prior in selected_tokens), default=0.0)
            value = base - 0.18 * similarity
            if value > best_value + 1e-12:
                best_value = value
                best_pos = pos
        chosen = remaining.pop(best_pos)
        base, original_rank, candidate = chosen
        meta = candidate.get("_isco_visual_intelligence") or {}
        meta["retrieval_score_v1"] = round(float(base), 6)
        meta["original_provider_rank"] = int(original_rank)
        candidate["_isco_visual_intelligence"] = meta
        selected.append(chosen)
        selected_tokens.append(_candidate_tokens(provider, candidate))
    return [item[2] for item in selected]


def _rerank_pool(candidates_by_provider: object, intent: VisualIntent) -> object:
    if not isinstance(candidates_by_provider, dict):
        return candidates_by_provider
    return {
        provider: rerank_provider_candidates(str(provider), list(items), intent)
        if isinstance(items, list) else items
        for provider, items in candidates_by_provider.items()
    }


def _install_pixabay_metadata_preservation() -> None:
    module = orchestrator.pixabay_provider
    current = getattr(module, "_normalize", None)
    if not callable(current) or getattr(current, "_isco_visual_intelligence_v1", False):
        return

    @wraps(current)
    def wrapped(hit: dict):
        candidate = current(hit)
        if isinstance(candidate, dict):
            candidate["_isco_visual_intelligence"] = {
                "tags": str(hit.get("tags") or ""),
                "views": hit.get("views"),
                "downloads": hit.get("downloads"),
                "likes": hit.get("likes"),
                "comments": hit.get("comments"),
                "picture_id": hit.get("picture_id"),
            }
            _annotate_candidate("pixabay", candidate)
        return candidate

    wrapped._isco_visual_intelligence_v1 = True
    wrapped._isco_visual_intelligence_original = current
    module._normalize = wrapped


def _wrap_search(current, provider: str):
    if getattr(current, "_isco_visual_retrieval_v1", False):
        return current

    @wraps(current)
    def wrapped(api_key: str, query: str, *args, **kwargs):
        results = current(api_key, query, *args, **kwargs)
        if not isinstance(results, list):
            return results
        intent = build_visual_intent(query)
        annotated = [_annotate_candidate(provider, item) if isinstance(item, dict) else item for item in results]
        valid = [item for item in annotated if isinstance(item, dict)]
        reranked = rerank_provider_candidates(provider, valid, intent)
        if reranked:
            top = reranked[0]
            meta = top.get("_isco_visual_intelligence") or {}
            print(
                "Visual Retrieval V1 search rerank: "
                f"provider={provider} results={len(reranked)} top_id={top.get('id')} "
                f"score={meta.get('retrieval_score_v1', 0)}"
            )
        return reranked

    wrapped._isco_visual_retrieval_v1 = True
    wrapped._isco_visual_retrieval_original = current
    return wrapped


def _install_search_wrappers() -> None:
    orchestrator.pexels_search_videos = _wrap_search(orchestrator.pexels_search_videos, "pexels")
    orchestrator.pixabay_provider.search_videos = _wrap_search(
        orchestrator.pixabay_provider.search_videos,
        "pixabay",
    )


def _wrap_selector(current, *, scope: str):
    if getattr(current, "_isco_visual_intent_scope_v1", False):
        return current

    @wraps(current)
    def wrapped(*args, **kwargs):
        intended = str(kwargs.get("intended_visual") or "").strip()
        intent = build_visual_intent(intended)
        token = _INTENT.set(intent)
        try:
            candidates = args[0] if args else kwargs.get("candidates_by_provider")
            reranked = _rerank_pool(candidates, intent)
            if args:
                args = (reranked, *args[1:])
            else:
                kwargs["candidates_by_provider"] = reranked
            result = current(*args, **kwargs)
            if isinstance(reranked, dict):
                preview = []
                for provider, items in reranked.items():
                    if isinstance(items, list) and items:
                        item = items[0]
                        meta = item.get("_isco_visual_intelligence") or {}
                        preview.append(f"{provider}:{item.get('id')}:{meta.get('retrieval_score_v1', 0)}")
                if preview:
                    print(f"Visual Retrieval V1 intent rerank: scope={scope} top={'|'.join(preview)}")
            return result
        finally:
            _INTENT.reset(token)

    wrapped._isco_visual_intent_scope_v1 = True
    wrapped._isco_visual_intent_original = current
    return wrapped


def _install_selector_scopes() -> None:
    current_opening = opening_director.select_opening_sequence
    wrapped_opening = _wrap_selector(current_opening, scope="opening")
    opening_director.select_opening_sequence = wrapped_opening
    if orchestrator.select_opening_sequence is current_opening:
        orchestrator.select_opening_sequence = wrapped_opening

    current_section = section_visual_sequence.select_section_sequence
    wrapped_section = _wrap_selector(current_section, scope="section")
    section_visual_sequence.select_section_sequence = wrapped_section
    if orchestrator.select_section_sequence is current_section:
        orchestrator.select_section_sequence = wrapped_section

    current_single = visual_selection.select_with_recovery
    wrapped_single = _wrap_selector(current_single, scope="single")
    visual_selection.select_with_recovery = wrapped_single
    if orchestrator.select_with_recovery is current_single:
        orchestrator.select_with_recovery = wrapped_single


def _reranking_rank_and_interleave(current):
    if getattr(current, "_isco_visual_rank_v1", False):
        return current

    @wraps(current)
    def wrapped(candidates_by_provider, *, portrait: bool, target_seconds: float, exclude_ids=None):
        intent = _INTENT.get()
        prepared = _rerank_pool(candidates_by_provider, intent) if intent is not None else candidates_by_provider
        return current(
            prepared,
            portrait=portrait,
            target_seconds=target_seconds,
            exclude_ids=exclude_ids,
        )

    wrapped._isco_visual_rank_v1 = True
    wrapped._isco_visual_rank_original = current
    return wrapped


def _install_rank_hooks() -> None:
    original = visual_selection.rank_and_interleave
    wrapped = _reranking_rank_and_interleave(original)
    visual_selection.rank_and_interleave = wrapped
    # These modules imported the function directly from visual_selection, so patch their
    # local call sites too. This keeps Long opening/body and standalone Short on one owner.
    opening_director.rank_and_interleave = wrapped
    section_visual_sequence.rank_and_interleave = wrapped


def _estimate_payload_tokens(payload: object) -> int:
    if not isinstance(payload, dict):
        return GROQ_IMAGE_TOKENS + GROQ_OUTPUT_TOKEN_RESERVE + 1800
    images = 0
    chars = 0
    for message in payload.get("messages") or ():
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chars += len(content)
            continue
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "image_url":
                images += 1
            elif item.get("type") == "text":
                chars += len(str(item.get("text") or ""))
    # Conservative char/token ratio for mixed English/Arabic prompt material.
    text_tokens = int(math.ceil(chars / 3.0))
    output_reserve = min(GROQ_OUTPUT_TOKEN_RESERVE, int(payload.get("max_completion_tokens") or GROQ_OUTPUT_TOKEN_RESERVE))
    return max(1, images * GROQ_IMAGE_TOKENS + text_tokens + output_reserve)


def _admit_groq(estimated_tokens: int, *, reserve_tokens: int = 0) -> float:
    """Delegate every Vision admission to the persisted model-scoped owner."""
    return shared_capacity.pace_groq_capacity(
        run181.GROQ_VISION_MODEL,
        int(estimated_tokens),
        reserve_tokens=max(0, int(reserve_tokens)),
        max_wait_seconds=GROQ_MAX_BOUNDED_WAIT_SECONDS,
        owner="vision",
    )


def _observe_groq_headers(response, estimated_tokens: int) -> None:
    shared_capacity.observe_groq_response(
        response,
        run181.GROQ_VISION_MODEL,
        required_tokens=int(estimated_tokens),
    )


def _observe_external_rate_limit(reason: object) -> None:
    shared_capacity.mark_groq_rate_limited(
        run181.GROQ_VISION_MODEL,
        reason,
        default_cooldown_seconds=60.0,
    )


def _is_daily_groq_limit(reason: object) -> bool:
    text = str(reason or "").casefold()
    return any(marker in text for marker in ("per day", "requests per day", "tokens per day", " rpd", " tpd", "daily limit"))


def _install_rate_health_semantics() -> None:
    current = health.publish_provider_unavailable
    if getattr(current, "_isco_visual_capacity_v1", False):
        return

    @wraps(current)
    def wrapped(provider: str, *, model: str = "*", quota_domain: str = "*", reason: str, source: str):
        normalized_provider = str(provider).strip().lower()
        if (
            normalized_provider == "groq"
            and str(model).strip() == run181.GROQ_VISION_MODEL
            and run181._quota_or_rate_failure(reason)
            and not _is_daily_groq_limit(reason)
        ):
            _observe_external_rate_limit(reason)
            print(
                "Vision Capacity V1: Groq short-window rate evidence converted to cooldown "
                f"source={source}"
            )
            return None
        return current(
            provider,
            model=model,
            quota_domain=quota_domain,
            reason=reason,
            source=source,
        )

    wrapped._isco_visual_capacity_v1 = True
    wrapped._isco_visual_capacity_original = current
    health.publish_provider_unavailable = wrapped


class _Run181RequestsProxy:
    """Observe only Run181's Groq HTTP boundary; never monkey-patch requests globally."""

    def __init__(self, base):
        self._base = base

    def __getattr__(self, name):
        return getattr(self._base, name)

    def get(self, *args, **kwargs):
        return self._base.get(*args, **kwargs)

    def post(self, url, *args, **kwargs):
        if str(url) != run181.GROQ_CHAT_URL:
            return self._base.post(url, *args, **kwargs)
        estimated = _estimate_payload_tokens(kwargs.get("json"))
        try:
            _admit_groq(estimated)
        except Exception as exc:
            if getattr(exc, "wire_attempted", None) is not False:
                raise
            reason_code = str(getattr(exc, "reason_code", ""))
            transient = reason_code in {
                "GROQ_TPM_WINDOW_BUSY_PRECHECK",
                "GROQ_TPM_WINDOW_EXCEEDS_WAIT_BUDGET",
            }
            detail = str(exc)
            if transient:
                # Keep the existing Vision mesh's rate classification and bounded
                # fallback behavior while preserving explicit zero-wire accounting.
                detail = f"HTTP_429 rate limit pre-admission {detail}"
            raise NoWireVisionStageError(
                (
                    contract.VisionErrorCode.PROVIDER_TRANSIENT
                    if transient
                    else contract.VisionErrorCode.CAPACITY
                ),
                detail,
                provider="groq",
                requested_model=run181.GROQ_VISION_MODEL,
                resolved_model=run181.GROQ_VISION_MODEL,
            ) from exc
        response = self._base.post(url, *args, **kwargs)
        _observe_groq_headers(response, estimated)
        return response


def _install_groq_transport_observer() -> None:
    current = run181.requests
    if isinstance(current, _Run181RequestsProxy):
        return
    run181.requests = _Run181RequestsProxy(current)


def _probe_duration(preview: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(preview),
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=CONTACT_SHEET_TIMEOUT_SECONDS,
    )
    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Visual contact sheet preview duration is invalid") from exc
    if duration <= 0:
        raise RuntimeError("Visual contact sheet preview duration is invalid")
    return duration


def _contact_sheet_bytes(preview: Path) -> bytes:
    preview = Path(preview)
    duration = _probe_duration(preview)
    fps = float(CONTACT_SHEET_FRAMES) / max(0.25, duration)
    with tempfile.TemporaryDirectory(prefix="isco-vision-contact-sheet-") as temp_dir:
        output = Path(temp_dir) / "contact-sheet.jpg"
        vf = (
            f"fps={fps:.8f},scale=min(320\\,iw):-2,"
            f"tile={CONTACT_SHEET_COLUMNS}x{CONTACT_SHEET_ROWS}:nb_frames={CONTACT_SHEET_FRAMES}:padding=2:margin=2"
        )
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(preview),
                "-vf", vf, "-frames:v", "1", "-q:v", "3", "-y", str(output),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=CONTACT_SHEET_TIMEOUT_SECONDS,
        )
        data = output.read_bytes()
    if not data or len(data) > CONTACT_SHEET_MAX_BYTES:
        raise RuntimeError("Visual contact sheet output size is invalid")
    return data


def _install_contact_sheet_evidence() -> None:
    current_sampler = contract.legacy._sample_preview_frames
    if not getattr(current_sampler, "_isco_contact_sheet_v1", False):
        @wraps(current_sampler)
        def contact_sheet_sampler(preview: Path):
            return [_contact_sheet_bytes(Path(preview))]

        contact_sheet_sampler._isco_contact_sheet_v1 = True
        contact_sheet_sampler._isco_contact_sheet_original = current_sampler
        contract.legacy._sample_preview_frames = contact_sheet_sampler

    current_prompt = contract.legacy._visual_prompt
    if not getattr(current_prompt, "_isco_contact_sheet_prompt_v1", False):
        @wraps(current_prompt)
        def contact_sheet_prompt(*args, **kwargs):
            base = current_prompt(*args, **kwargs)
            return (
                base
                + "\n\nFALLBACK TEMPORAL EVIDENCE: if one image is attached, it is a 3x2 contact sheet "
                  "of six chronological frames sampled across the same clip. Treat every tile as evidence "
                  "from that one clip; fail closed if the grid is insufficient to establish any mandatory condition."
            )

        contact_sheet_prompt._isco_contact_sheet_prompt_v1 = True
        contact_sheet_prompt._isco_contact_sheet_prompt_original = current_prompt
        contract.legacy._visual_prompt = contact_sheet_prompt


def _install_contract_fingerprint() -> None:
    current = contract.vision_contract_fingerprint
    if getattr(current, "_isco_visual_retrieval_v1", False):
        return

    @wraps(current)
    def wrapped() -> str:
        payload = {
            "base": current(),
            "contract_id": CONTRACT_ID,
            "contract_version": CONTRACT_VERSION,
            "module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "contact_sheet": [CONTACT_SHEET_COLUMNS, CONTACT_SHEET_ROWS, CONTACT_SHEET_FRAMES],
            "groq_image_tokens": GROQ_IMAGE_TOKENS,
            "groq_tpm_hint": GROQ_FREE_TPM_HINT,
            "ranking": "provider-metadata+intent-expansion+mmr",
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    wrapped._isco_visual_retrieval_v1 = True
    wrapped._isco_visual_retrieval_original = current
    contract.vision_contract_fingerprint = wrapped


def recall_at_k(ranked_ids: list[object], relevant_ids: set[object], k: int) -> float:
    if not relevant_ids:
        return 1.0
    return len(set(ranked_ids[: max(0, int(k))]) & set(relevant_ids)) / float(len(relevant_ids))


def reciprocal_rank(ranked_ids: list[object], relevant_ids: set[object]) -> float:
    for index, item in enumerate(ranked_ids, 1):
        if item in relevant_ids:
            return 1.0 / float(index)
    return 0.0


def install_visual_retrieval_adjudication_v1() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _install_pixabay_metadata_preservation()
    _install_search_wrappers()
    _install_selector_scopes()
    _install_rank_hooks()
    _install_rate_health_semantics()
    _install_groq_transport_observer()
    _install_contact_sheet_evidence()
    _install_contract_fingerprint()
    _INSTALLED = True
    print(
        "Visual Retrieval & Adjudication V1 installed: local intent-aware metadata rerank+MMR; "
        "Pixabay/Pexels intelligence preserved; shared persisted Groq TPM admission; 6-frame single-image "
        "contact sheet; Long+Short shared Vision gates unchanged"
    )
