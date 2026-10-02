# Clean V2 Short s3 contract

## Canonical ownership

Short s3 is structured before it is spoken:

- `s3_payoff`: Script-owned descriptive payoff.
- `s3_locked_action`: host-owned copy of Planning's `practical_action_ar`.
- `narration`: materialized only after the two fields pass the local Short s3 validator.

Tone/factuality repair may patch `s3_payoff` only. It must never rewrite, paraphrase, or replace `practical_action_ar` / `s3_locked_action`. A patch that tries to touch the locked action is rejected locally and terminates that patch route without trying another AI provider.

The production Short path no longer chains sentence-level s3 action/payoff rescue gates. Stored pre-refactor artifacts may still be adapted locally for compatibility, but the canonical validator always receives the two fields separately.

## Provider-call boundary

This refactor does not change Planning fallback order. Planning can still traverse the existing bounded route:

1. Gemini
2. Gemini Flash Lite
3. Groq
4. OpenRouter
5. Mistral

The purpose of the s3 refactor is narrower: prevent text-audit repair from spending additional providers on a structural payoff/action conflict that the host can resolve deterministically.

## Unchanged critical gates

This change does not alter the logic of the existing critical quality gates for:

- hook quality
- Arabic/tone naturalness
- coherence / editorial dependency
- safety / factuality protections
- obvious duplication / repetition
