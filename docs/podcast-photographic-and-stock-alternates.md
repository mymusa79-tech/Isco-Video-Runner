# Approved ZIP editor contract: three stock links per scene, images as fallback

## New packages: **every authored scene** gets three distinct stock page URLs

For each newly authored «نداء اليقظة» Short or Film ZIP (and any «على مهل» story explicitly using the same stock route), set the optional-to-legacy but **mandatory-for-new-Nidaa-authoring** document field. Podcast may instead use its independent natural-photo-first plan without activating this stock policy:

```json
"stock_policy": "three_urls_per_scene"
```

Each entry in `scenes` must contain both its exact approved **fallback image** at `asset` and **three distinct, editorially verified Pexels/Pixabay/Coverr page links** in priority order:

```json
{
  "id": "scene_01",
  "section_id": "s1",
  "start_text": "نص عربي مطابق لبداية اللحظة المنطوقة",
  "asset": "assets/scene_01.jpg",
  "motion": "hold",
  "transition": "cut",
  "stock": {
    "urls": [
      "https://www.pexels.com/video/REPLACE_WITH_REAL_FIRST_CLIP-1234567/",
      "https://www.pexels.com/video/REPLACE_WITH_REAL_SECOND_CLIP-2345678/",
      "https://pixabay.com/videos/REPLACE_WITH_REAL_THIRD_CLIP-3456789/"
    ],
    "must_show": "The same visible action described by this exact scene"
  }
}
```

**The URLs above are illustrative placeholders, NOT verified, usable stock links.** Do not ship them. Search the stock websites and inspect the actual three pages, rights, visible meaning, orientation and whether they are real downloadable stock. Never invent an asset ID or count a search query as a URL. The three candidates express the **same scene intent**, rather than three clips to play sequentially. Each authored scene has its own three links; links cannot duplicate across different scenes.

The renderer attempts link 1 → 2 → 3. Once a candidate downloads, passes media preflight/technical checks and is admitted, it uses that candidate. For video candidates that cannot cover the estimated scene duration in a three-link pack, the next URL is tried. **Only after all three are unavailable or unsuitable does the scene use its approved fallback image.** Historical single-link ZIPs keep their legacy short-clip-then-image continuation. The new policy also prevents a synthetic image-only beat being inserted after the opening hook. Technical validation of link syntax is not live verification, nor proof that a chosen visual truly matches the words. Human editorial review remains mandatory. Technical validation of link syntax is not live verification, nor proof that a chosen visual truly matches the words. Human editorial review remains mandatory.

New-authoring `stock_policy` makes the importer **reject** incomplete coverage: any scene missing `stock.urls`, or providing fewer/more than three, or repeating a clip, is invalid. Under this policy every scene may have stock; the old podcast 16-scene/«على مهل» 2-scene stock caps apply only to historical manifests.

## «خارج النص» / podcast: natural photos first; not the long-film montage

- The A/B conversation carries the meaning. Start with the **entire first A question/turn**, including its internal sentences; only then the existing 6-second V8 intro, approved prayer, and B's answer.
- Aim for **4–7 distinct photographic stills per major section** only where the ideas call for them. These are natural, observational scenes (actual everyday objects/places/actions) and a **target**, not a rigid montage quota. Optional podcast stock candidates, if a scene specifically needs them, should preferentially be Pexels/Pixabay **photograph pages** or very calm footage, not dynamic film cutaways. Without a stock request, use the approved photograph normally; if requested stock fails, use the fallback photograph. Three links are **not required** for naturally photographed podcast coverage.
- Use mostly `motion: "hold"` and `transition: "cut"`, with only occasional restrained dissolve or gentle camera motion. No film-like zoom punches, boards, fast montage, heavy navy/gold filters or forced edits at each speaker alternation. Change stills only when the spoken idea advances.
- The old one-image podcast ZIP remains importable for backward compatibility but **cannot improve visual variety without a new photo plan**.

## «نداء اليقظة» ordinary Short and Film, and «على مهل»

- Ordinary Short/Film: use the same three-real-URL contract per authored scene, with grounded footage matching the spoken text. A distinct fallback image is always included in `assets/`. No repeated generic footage, no reverse, no clips with recognisable faces/logos.
- «على مهل»: three links per authored story scene **without changing characters, clothing or causal story continuity**. Prefer unobtrusive setting/detail stock, and coherent owner-generated fallback photographs; a technically reachable but mismatched clip is **not** editorial approval. The importer retains its safety rule around scenes that continue their image across later sections.
- The old one-URL `stock.url` and `query_en` search route continue to import **only for older ZIPs without** `stock_policy`. Those older ZIPs do not meet the new channel-authoring standard.

## Ownership / verification

- ZIP contains `production.json` and `assets/`; the approved image is a real readable included file, never a promised image. A ChatGPT-created cover is an independent image asset.
- Claude reviews the **actual working URLs and exact script anchors** before APPROVED, then user alone manually imports and publishes. No paid providers or auto-merge.
- Structural validation guarantees **three declared, uniquely identified Pexels/Pixabay/Coverr page URLs per scene**, not that a third-party provider serves them at production time. The report records which link was admitted or why all failed. No unsupported guarantee of viewer retention.
