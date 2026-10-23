# Owner-selected YouTube Audio Library instrumentals — V1

## Purpose and status

This is an **opt-in** owner-supplied music bed for an approved ZIP; it uses the
existing Clean V2 topic-only mixer and real audio measurements. No engine, music
provider, subscription, automatic selection or new TTS is added.

The model/editor chooses one track and the exact initial delay. The runner never
replaces that choice with FreePD/Coverr. Legacy `editing: {"music":"none"}`
remains byte-for-byte unchanged.

## Private six-track registry (no audio in public GitHub)

| track_id | mood / eligible templates |
| --- | --- |
| `forgiven-fate` | warm story; `ala_mahl`, Nidaa `short` |
| `demoiselle` | warm/light; `ala_mahl`, Nidaa `short` |
| `lift-up` | hopeful ascent; Nidaa `short`, `film` |
| `after-sunset` | quiet dialogue; `podcast`, `film` |
| `nebula` | restrained wonder; `podcast`, `film` |
| `in-battery` | subtle tension; `film` |

**Legal and editorial gate:** these are specific files the owner uploaded,
described as YouTube Audio Library downloads. The runner verifies the exact
owner-supplied *transcoded* MP3 SHA-256; this does **not** establish copyright
license status by itself. The owner must verify the **License type** and any
attribution requirements in YouTube Studio for each chosen track before
publishing. Audio Library music may be used in monetized YouTube uploads under
the official terms. Do not assume usage rights elsewhere.

### Production JSON

For one approved video (after genuine Claude APPROVED), include the selected
audio file `assets/music_bed.mp3` in **that same private ZIP** with the
ordinary photographic scene assets and `production.json`. The sound is an
optional asset and does not change the voice or dialogue.

```json
"editing": {
  "music": "youtube_audio_library",
  "track_id": "after-sunset",
  "asset": "assets/music_bed.mp3",
  "delay_seconds": 3
}
```

Replace with `"editing": {"music": "none"}` for music-free production.

- The editor sets `track_id` and `delay_seconds` (1–12 seconds).
- `delay_seconds` begins after the exact `topic` event in the **measured voice
  timeline**, never over the pre-topic hook/identity/prayer.
- The mixer normalizes music to about −27 dB (Short), −29 dB (Film) or −30 dB
  (Podcast) relative to mean narration. It uses fades/limited dynamics, and
  stops at the earlier of the end of the track and the end of the subject;
  **no repeat music loop** for long dialogue.
- No soundtrack is inserted into outro or after the subject. It changes only
  the finalized audio mix, not the master recording, words, image timing, CTA,
  identities, prayer or publisher settings.
- A missing or mismatched MP3 is rejected at ZIP intake, not secretly replaced
  with another track. The private ZIP must still obey the **18 MiB / 150 files**
  limit. Use the owner-provided 128 kbps cue, not the much larger original.
- No remote downloads, public music assets, keys or credentials.

### One-time editor workflow

1. The owner keeps the private `nidaa_music_pack_v1.zip` and checks YouTube
   Audio Library license type for each selected file.
2. The editor chooses one cue that matches the **scene/meaning**, not simply
   a generic mood, and puts its exact transcoded bytes in
   `assets/music_bed.mp3`. No other audio may be added to the ZIP.
3. The editor adds the exact JSON above, reviews narration, sends the private
   ZIP for genuine Claude review, and publishes only by the owner after
   APPROVED and final MP4 listening.
4. Inspect `topic-audio-polish.json`: `status=pass`,
   `music_added=true`, `voice_changed=false`, matching track_id,
   `external_download_required=false`. A technical pass alone never
   guarantees a comfortable listening level; preview on phone speakers.

### Scope

**Only owner-selected music beds.** Foley / effects are not included in V1.
The user does not need to paste music into GitHub Actions or maintain a paid
third-party music service. Historical ZIPs and published videos remain
unchanged.
