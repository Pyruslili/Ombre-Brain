# Infra Notes

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
