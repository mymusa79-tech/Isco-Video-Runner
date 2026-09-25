# Director/Layout Tightening V1 — Short audio crackle diagnosis

Date: 2026-09-25
Reference: the latest user-reviewed Short production (51.2 s, 1080x1920).

## Finding

The annoying crackle/noise is not produced by narration mastering.

The post-master `clean_v2.short_audio_polish` layer deliberately synthesizes broadband noise:
- the fallback music bed mixes FFmpeg `anoisesrc=color=pink` and `anoisesrc=color=brown`;
- the hook accent takes the `frequency < 600` branch and therefore becomes another pink-noise source (the production hook requests 523.25 Hz);
- narration mastering is explicitly left untouched by this stage.

That makes the additive polish layer the direct and reproducible source family for the reported crackle. Lowering mastering gain would only hide the symptom while leaving the noisy source in place.

## Required correction

1. Remove procedural pink/brown-noise music from Short production.
2. Remove the pink-noise hook accent and the synthetic payoff accent from this layer.
3. Use a real, locally cached, license-documented music track only.
4. Mix music only during the measured `topic` window: never during Hook, Intro, prayer/channel identity, or Outro/final silence.
5. Keep the music at -25 to -20 dB relative to the mastered narration.
6. Preserve the mastered narration unchanged.

This diagnosis is intentionally committed before the audio correction so the cause and the fix remain independently auditable.
