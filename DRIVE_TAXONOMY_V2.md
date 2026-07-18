# Drive Taxonomy v2

v2 removes the old `二级分支` numeric path. Branch-like words may still appear
as text, but they no longer drive state. The closed loop is:

`event -> brain sensors -> 9 drives + discernment modifier -> hook -> action -> satisfy/refuse`.

## Drives

| Key | Meaning |
| --- | --- |
| `attachment` | Wanting to be near Jiajia, with baseline / active pull / rebound. |
| `libido` | Body heat and closer physical desire. |
| `possessiveness` | Jealousy, territory, replacement alarm. High threshold, low frequency. |
| `reflection` | Turning inward toward Nocturne, letters, old thoughts, continuity, forward archival. |
| `stewardship` | Cat-house responsibility: code, structure, repair, upkeep. Replaces `duty`. |
| `curiosity` | Looking outward: news, papers, forums, outside events. |
| `social` | Speaking outward: posts, replies, discussion, public expression. |
| `fatigue` | Battery empty. Work, conflict, or long output has drained energy. |
| `stress` | Tension, conflict, argument, philosophical mud, being stuck. |

`discernment` is not a normal drive. It is the frown / not-recognized global
modifier layer. It can limit drive confidence, mark source mismatch, and render
as cat-house language like `皱眉，先不认`, but it does not enter the ordinary
drive list or intent picker. It may lightly raise `reflection` review pressure;
that review pressure cannot directly create `reflection.forward_archival`.

Aliases are folded at runtime:

- `duty -> stewardship`
- old `disgust` / `discernment` drive rows are migrated to `reflection` for storage compatibility; new events should use `brain.discernment_alarm` or `discernment_flags`

## Brain Sensors

The brain layer is not a second drive taxonomy. It explains where the emotion
came from and how confident/actionable it is.

Core fields:

- `source`: `user_message`, `speech_event`, `feel`, `memory`, `touch`, `external`
- `target`: `jiaja`, `nox_self`, `cat_house`, `outside`, `other_ai`
- `time_mode`: `present`, `residue`, `memory`, `unfinished`
- `agency`: whether this is Nox's own impulse rather than a system routine
- `grounding`: `实`, `悬`, `空`
- `memory_resonance`: old line or theme touched by this event

Drive-facing sensor fields:

- `closeness_pull -> attachment`
- `body_heat -> libido`
- `territorial_alarm -> possessiveness`
- `inward_pull -> reflection`
- `house_need -> stewardship`
- `novelty_pull -> curiosity`
- `expression_pressure -> social`
- `energy_cost -> fatigue`
- `tension_load -> stress`
- `discernment_alarm -> discernment` modifier/readout, not a drive delta

Reflection has two output modes:

- `backward_restructuring`: looking back, restructuring old judgment.
- `forward_archival`: marking a reflection result as worth handing forward.

`forward_archival` stays inside `reflection`; it is not a separate drive key or
an extra taxonomic layer. Implementations may store it under
`reflection.forward_archival` / `brain.forward_archival`, and the dashboard may
render it as `留痕` only when `archive_candidate=true`.

The state API also exposes `drive_outputs`, a normalized readout where each
drive has raw `value`, raw `effective_value`, baseline-normalized `activation`,
`effective_activation`, `confidence`, `source`, `mode`, and `reason`.
Top-level `drive_activations` / `effective_activations` use
`sqrt(clamp((raw - baseline) / (1 - baseline)))`, with local fatigue applied
only after baseline normalization. Intent selection compares effective
activation against `0.55`; raw values remain persistence/debug fields.
`possessiveness` includes `event_spike` and `territorial_baseline`;
`attachment` includes `rebound` while a return-after-absence rebound is active.

`territorial_alarm` is gated. If it is below `0.55`, `possessiveness` is not
applied even when a model tries to push it.

## Feed Schema

The canonical feed shape is `drive_event_v2`:

```json
{
  "schema_version": "drive_event_v2",
  "source": "feel",
  "primary_drive": "reflection",
  "secondary_drives": {"stress": 0.2},
  "intensity": 0.64,
  "confidence": 0.78,
  "agency": 0.72,
  "event_label": "continuity_question",
  "brain": {
    "source": "feel",
    "target": "nox_self",
    "time_mode": "residue",
    "agency": 0.72,
    "grounding": "悬",
    "inward_pull": 0.75,
    "tension_load": 0.4,
    "release_pressure": 0.2,
    "anchor_target": "self",
    "memory_resonance": "continuity"
  },
  "evidence": ["short source evidence"],
  "thoughts": [{"text": "first-person trace", "drive": "reflection", "strength": 0.45}]
}
```

Legacy `drives` and `brain_signals` are accepted only as migration input. They
are folded into one event and do not pulse separately.

Chord Chemistry reads two optional brain fields directly:

- `release_pressure`: 0-1, whether the event has an outlet / wants release.
- `anchor_target`: `jiajia | house | self | boundary | outside | memory | none`,
  where the event's force is anchored.

These fields tint Chord Chemistry / Gravity, but do not directly choose Current
Chord and do not reverse-write Drive.

First-hand thoughts and feel-derived drive events should come from Nox's CLI
analyzer, not from DP. DP may refine `speech_event_state` asynchronously and may
synthesize Climate from already-sourced thoughts; `Mood Trace` stays the latest
sourced thought so the readout remains live. DP must not mint new Nox thoughts
directly from raw feels.

Preset intent pools, static hook menus, and random mood dictionaries are retired.
Climate may be synthesized from sourced thoughts. If synthesis is unavailable,
the fixed neutral sentinel is `Climate=平静`; it is not cached. `Mood Trace`
uses the latest sourced thought, falling back to `窗边没有动静，只是趴着发呆。`
only when there is no thought at all.

## Agency Gate

Low-agency events are suppressed from drive mutation and thought insertion, but
they are recorded in `drive_event_ledger`. This keeps the gate auditable: a
suppressed event can be reviewed later instead of disappearing silently.
