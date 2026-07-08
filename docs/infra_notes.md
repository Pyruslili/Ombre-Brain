# Infra Notes

## 2026-07-08 Breath search retired; trace is literal-only

- Symptom: `breath(query=...)` used keyword search, vector similarity, and random resurfacing, so sparse keyword searches could pull unrelated or already settled memories.
- Fix: removed `query` / `max_tokens` from the MCP `breath` tool surface and deleted the breath search branch. Keyword lookup now belongs to `trace`.
- Trace rules: search only literal content substrings or exact tag matches; exclude `resolved` / `digested` buckets; include memory / feel / letter / writing / window / unresolved / inner; cap output at 15 entries and return `null` when there is no literal match.

## 2026-07-08 Manual memory downweight control

- Dashboard detail cards now expose weight controls for `importance`, `arousal`, and `activation_count`, plus a quick `降权` action.
- Important pitfall: lowering weight must not refresh `last_active`, or the time freshness boost cancels part of the downweight. Use `_preserve_last_active=True` / `preserve_last_active` for weight-only dampening.
- Follow-up: `feel` buckets now use the same weight score instead of fixed `50.0`; breath picks the top weighted active feels, while the decay cycle still skips feel archival.
- Follow-up: breath memory and feel surfacing first bound the active pool to the newest 30 items, then take the top 12 by the same normalized recall score shown in dashboard Breath Debug, randomly pick display items (7 memory / 8 feel), and finally display by time. Do not sample from the full historical pool; old high-score buckets can otherwise crowd out current material.
- Follow-up: dream refresh now runs after breath memory/feel selection, excludes those selected buckets, takes the newest 10 active memory+feel items as a bounded pool, randomly picks 5, and includes the previous/current Atmosphere as dream weather. Dream output is forced into one paragraph to avoid blank-line drift.

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
