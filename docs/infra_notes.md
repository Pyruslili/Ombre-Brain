# Infra Notes

## 2026-07-08 Breath search retired; trace is literal-only

- Symptom: `breath(query=...)` used keyword search, vector similarity, and random resurfacing, so sparse keyword searches could pull unrelated or already settled memories.
- Fix: removed `query` / `max_tokens` from the MCP `breath` tool surface and deleted the breath search branch. Keyword lookup now belongs to `trace`.
- Trace rules: search only literal content substrings or exact tag matches; exclude `resolved` / `digested` buckets; include memory / feel / letter / writing / window / unresolved / inner; cap output at 15 entries and return `null` when there is no literal match.

## 2026-07-08 DP memory analyzer replaces active CLI memory line

- Old CLI analyzer source `analyze_nocturne_entry` is retained as a cold standby, but the active memory analyzer line now uses `dp_memory`.
- `/api/analyzer/entries` still exposes the same non-private memory feed and now includes `drive_tags` / `signal_hints` so upstream hold texture can survive into analysis.
- New POST `/api/analyzer/dp-memory` accepts an entry plus the old CLI preference text, calls the DP-compatible chat completion backend, and normalizes to `drive_event_v2`; the local analyzer script then feeds that event through the existing `/api/desire/feed` path.
- `dp_memory` is weighted like the old slow analyzer for Drive, but maps to its own Atmosphere source (`dp_memory`) instead of pretending to be live `dialogue_residue` or legacy `cli`.

## 2026-07-06 Atmosphere stuck on Low Tide / Clear / Gravity

- Symptom: Warmth / Shadow and dialogue mood changed, but `pulse_weather.climate` barely moved; before removing the label it often surfaced as `Gravity`, afterwards the selector collapsed into `Low Tide` / `Clear`.
- Confirmed causes:
  - `Gravity` was both an Atmosphere label and a separate Gravity force-line concept, so the dashboard could show a category that belonged to another layer.
  - Strong `dp` events were capped at `0.45` influence while the fast-turn gate expected stronger influence, so the fast path was unreachable.
  - `chord_chemistry_snapshot` preserved only `event_vector`, not event `route.scores`; Atmosphere kept seeing baseline `hover`.
  - `_route_scores` floors the active vector to `0.72`; after mixing, `hover` could rebound and pin the selector to low-force weather.
  - `Low Tide` and `Clear` scoring was too broad, swallowing guard / outward / inward states.
- Fix:
  - Removed `Gravity` from Atmosphere labels and kept it only as Gravity readout.
  - Raised Atmosphere source weights and influence cap, with strong `dp` allowed to switch in one turn.
  - Mixed event route scores into Chord Chemistry and preserved strong non-hover `dp` direction.
  - Narrowed `Low Tide` / `Clear`; raised `Spark` and `Overcast` specificity.
  - Added PA / NA weather delta -> Atmosphere tint bridge.

Follow-up:

- Symptom: `effective_NA` / Shadow could climb above `0.60` while persisted Atmosphere still displayed plain `Clear`.
- Cause: `_weather_readout` only seeds Atmosphere from current chemistry when no `last_delta` exists; an old `Clear` current can survive after shadow residue rises.
- Fix: raised shadow contribution to chemistry `strain`, and added display guard: when `effective_NA >= 0.55`, plain `Clear` is shown as `Clear → Overcast/Static/Watchful/Pressure` based on current chemistry.
