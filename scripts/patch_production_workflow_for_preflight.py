from __future__ import annotations

from pathlib import Path


PATH = Path(".github/workflows/produce-resilient-v4.yml")


def transition(text: str, old: str, new: str) -> str:
    old_count = text.count(old)
    new_count = text.count(new)
    if old_count == 1 and new_count == 0:
        return text.replace(old, new, 1)
    if old_count == 0 and new_count == 1:
        return text
    raise RuntimeError(
        f"workflow transition ambiguous old={old_count} new={new_count}: {old[:80]!r}"
    )


def replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"expected exactly one workflow marker, found {count}: {old[:80]!r}")
    return text.replace(old, new, 1)


def main() -> None:
    text = PATH.read_text(encoding="utf-8")
    text = transition(text, "    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-24.04\n")
    text = transition(
        text,
        "      - name: Checkout private engine\n",
        "      - name: Verify exact Runner checkout\n"
        "        run: test \"$(git rev-parse HEAD)\" = \"$GITHUB_SHA\"\n\n"
        "      - name: Checkout private engine\n",
    )
    text = transition(
        text,
        "          python -m pip install 'piper-tts==1.4.2'\n",
        "          python -m pip install 'piper-tts==1.4.2'\n"
        "          python -m pip check # post-piper-certification\n",
    )
    text = transition(
        text,
        "      - name: Verify local voice fallback before cloud production\n",
        "      - name: Require healthy restored cross-run memory\n"
        "        run: test \"${{ steps.restore_state.outputs.save_allowed }}\" = \"true\"\n\n"
        "      - name: Verify local voice fallback before cloud production\n",
    )

    old_provider = '''      - name: Verify free provider authentication
        id: verify_providers
        env:
          GROQ_API_KEY_FILE: ${{ runner.temp }}/isco-secrets/groq
          PIXABAY_API_KEY_FILE: ${{ runner.temp }}/isco-secrets/pixabay
        run: |
          python - <<'PY'
          import os, requests
          from pathlib import Path
          groq = Path(os.environ["GROQ_API_KEY_FILE"]).read_text().strip()
          pixabay = Path(os.environ["PIXABAY_API_KEY_FILE"]).read_text().strip()
          a = requests.get("https://api.groq.com/openai/v1/models", headers={"Authorization": "Bearer " + groq}, timeout=30)
          if not a.ok: raise SystemExit(f"Groq auth failed: HTTP {a.status_code}")
          b = requests.get("https://pixabay.com/api/videos/", params={"key": pixabay, "q": "nature", "per_page": 3, "safesearch": "true"}, timeout=30)
          if not b.ok: raise SystemExit(f"Pixabay auth failed: HTTP {b.status_code}")
          print("Groq auth OK; Pixabay auth OK")
          PY
'''
    new_provider = '''      # Release namespace preflight: existing release tag blocks this run before production.
      - name: Verify complete provider and environment readiness
        id: verify_providers
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GEMINI_API_KEY_FILE: ${{ runner.temp }}/isco-secrets/gemini
          GROQ_API_KEY_FILE: ${{ runner.temp }}/isco-secrets/groq
          OPENROUTER_API_KEY_FILE: ${{ runner.temp }}/isco-secrets/openrouter
          PEXELS_API_KEY_FILE: ${{ runner.temp }}/isco-secrets/pexels
          PIXABAY_API_KEY_FILE: ${{ runner.temp }}/isco-secrets/pixabay
          GEMINI_CONTENT_MODEL: gemini-2.5-flash
          GEMINI_TTS_MODEL: gemini-3.1-flash-tts-preview
        run: |
          set -euo pipefail
          python scripts/environment_preflight.py
          python scripts/provider_preflight.py \\
            --output "$RUNNER_TEMP/provider-preflight.json" \\
            --content-model "$GEMINI_CONTENT_MODEL" \\
            --tts-model "$GEMINI_TTS_MODEL" \\
            --gemini-key-file "$GEMINI_API_KEY_FILE" \\
            --groq-key-file "$GROQ_API_KEY_FILE" \\
            --openrouter-key-file "$OPENROUTER_API_KEY_FILE" \\
            --pexels-key-file "$PEXELS_API_KEY_FILE" \\
            --pixabay-key-file "$PIXABAY_API_KEY_FILE"
'''
    text = transition(text, old_provider, new_provider)

    text = transition(
        text,
        "            engine/output/*/delivery-manifest.json\n",
        "            engine/output/*/delivery-manifest.json\n"
        "            ${{ runner.temp }}/provider-preflight.json\n"
        "            ${{ runner.temp }}/preproduction-environment.json\n",
    )

    text = transition(
        text,
        "      - name: Persist approved encrypted cross-run memory\n"
        "        id: persist_state\n"
        "        if: success() && steps.publish_approval.outputs.effective_decision == 'approved' && steps.restore_state.outputs.save_allowed == 'true'\n"
        "        continue-on-error: true\n",
        "      - name: Persist approved encrypted cross-run memory\n"
        "        id: persist_state\n"
        "        if: success() && steps.publish_approval.outputs.effective_decision == 'approved' && steps.restore_state.outputs.save_allowed == 'true'\n",
    )
    text = transition(
        text,
        "          python scripts/persistent_memory.py persist \\\n"
        "            --repo state-writer \\\n"
        "            --encrypted \"$encrypted\" \\\n"
        "            --branch agent-state \\\n"
        "            --run-number \"$GITHUB_RUN_NUMBER\"\n",
        "          python scripts/state_persistence_strict.py \\\n"
        "            --repo state-writer \\\n"
        "            --encrypted \"$encrypted\" \\\n"
        "            --branch agent-state \\\n"
        "            --run-number \"$GITHUB_RUN_NUMBER\" \\\n"
        "            --report \"$RUNNER_TEMP/state-persistence.json\"\n",
    )

    text = transition(
        text,
        "      - name: Notify Telegram\n",
        "      - name: Upload state closure diagnostics\n"
        "        if: always() && steps.persist_state.outcome == 'failure'\n"
        "        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a\n"
        "        with:\n"
        "          name: isco-state-closure-${{ github.run_number }}\n"
        "          path: ${{ runner.temp }}/state-persistence.json\n"
        "          if-no-files-found: warn\n"
        "          retention-days: 7\n\n"
        "      - name: Notify Telegram\n",
    )
    text = transition(
        text,
        "          CREATE_RELEASE_OUTCOME: ${{ steps.create_release.outcome }}\n",
        "          CREATE_RELEASE_OUTCOME: ${{ steps.create_release.outcome }}\n"
        "          PERSIST_STATE_OUTCOME: ${{ steps.persist_state.outcome }}\n",
    )
    text = transition(
        text,
        "              \"Create release:${CREATE_RELEASE_OUTCOME}\"\n",
        "              \"Create release:${CREATE_RELEASE_OUTCOME}\" \\\n"
        "              \"Persist accepted state:${PERSIST_STATE_OUTCOME}\"\n",
    )

    PATH.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
