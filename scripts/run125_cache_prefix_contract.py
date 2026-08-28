from __future__ import annotations

from scripts import run125_capacity_routing_closure as closure


def _writer_cache_layout(prompt: str) -> str:
    """Move every writer shard-specific field behind the common prompt prefix.

    Groq prompt caching is exact-prefix based. The existing writer prompt starts with a
    stable role sentence but puts `Write ONLY global sections X-Y` immediately after it,
    so every shard diverges before the expensive shared policy/research/persona body.
    Preserve the original text verbatim apart from whitespace at block joins and move
    only shard-specific context to the tail.
    """
    text = prompt
    dynamic: list[str] = []

    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.startswith("Write ONLY global sections "):
            dynamic.append(lines.pop(index))
            text = "".join(lines)
            break

    text, block = closure._extract_block(
        text,
        "PREVIOUS_WRITTEN_KEY_POINTS (context only; do not repeat their role):",
        "FOLLOWING_SECTION_PURPOSES (context only; do not steal their payoff):",
    )
    if block:
        dynamic.append(block)
    text, block = closure._extract_block(
        text,
        "FOLLOWING_SECTION_PURPOSES (context only; do not steal their payoff):",
        "Hard writing rules for every returned section:",
    )
    if block:
        dynamic.append(block)
    text, block = closure._extract_block(text, "GLOBAL POSITION RULES:", "EDITORIAL_POLICY:")
    if block:
        dynamic.append(block)

    batch_start = text.find(
        "BATCH_SECTION_SPECS — write exactly one narration per entry in this exact order:"
    )
    if batch_start >= 0:
        dynamic.append(text[batch_start:])
        text = text[:batch_start]

    if not dynamic:
        return prompt
    return (
        text.rstrip()
        + f"\n\n{closure._CACHE_LAYOUT_MARKER}\n"
        + "DYNAMIC_BATCH_CONTEXT — transport-specific values follow the shared cached prefix:\n"
        + "\n".join(part.strip() for part in dynamic if part.strip())
        + "\n"
    )


def install_run125_cache_prefix_contract() -> None:
    closure._writer_cache_layout = _writer_cache_layout
    print(
        "Run125 cache-prefix contract installed: "
        "writer_range_and_shard_state_after_shared_policy=true"
    )
