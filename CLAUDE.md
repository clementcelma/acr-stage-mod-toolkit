# ACR stage mod toolkit

A public GitHub repo of standalone tools for building Assetto Corsa Rally stages. Each tool is a folder at the root
(`pacenotes-generator/`, ...) with its own `README.md` and `requirements.txt`.

The tools are **extracted from the private pipeline `../acr-stage-importer`**, where they were developed and tested on
real stages. That repo is the source; this one is the shareable copy.

**Game version: Assetto Corsa Rally 0.6 (Early Access).** Every README says so; after a game update, re-read the
game data a tool depends on (e.g. `DT_PacenoteTokens_EA` -> `pacenotes-generator/acr_tokens.json`) and bump it.

## Rules

- **Standalone.** A tool imports nothing from acr-stage-importer: no `stage_paths`, no `stage.json` /
  `acr_export.json`, no `run.py`, no Unreal. Every input is a command-line argument (file paths, language); every
  output goes to `--out`. A shared helper is copied into the tool's folder (`pacenotes-generator/rally_pace.py`), never
  imported across tools.
- **Same logic as the source.** Extraction changes only I/O. After every port, run the tool on a stage the source
  pipeline already processed and diff the outputs; any difference must be explained before it ships.
- **Nothing private.** No personal names, emails, local paths (`C:\Users\...`, `E:\WORK\...`), stage data, videos,
  transcripts or screenshots in the repo. Test data stays in acr-stage-importer; `.gitignore` covers `work/`, `out/`,
  `*.mp4`, `*.json` outputs.
- **Windows users:** every command in a README is ONE line (works in PowerShell and cmd). No bash continuations.
- Pure Python + numpy where possible. Heavy dependencies (faster-whisper ...) only in the step that needs them.
- A README states what goes in, what comes out, and the known limits, measured, with numbers.

## Tools and where they come from

| Tool | Source in acr-stage-importer | Verified |
|---|---|---|
| `pacenotes-generator/transcribe.py` | `core/chain/136_pacenote_transcribe.py` (+ GPU fallback from `core/pipeline/transcribe_audio.py`) | |
| `pacenotes-generator/pacenotes.py` | `core/chain/144_pacenotes.py` (+ ACR tokens from `core/chain/133_acr_additive.ps1` and acr-extrastages `config/pacenote-map.toml`) | 2026-11-19 extraction: Cruz Tejeda (es, `--sync`) identical row for row, Whaanga Coast (fi) identical to `144 --dry`. **Then improved here, AHEAD of 144** (see below) |
| `pacenotes-generator/rally_pace.py` | `core/pipeline/rally_pace.py` | copy |

**A fix in either place must be ported to the other**, or the two copies drift apart. When editing a tool here, check
whether the source script changed since the date in the table.

### pacenotes.py is ahead of 144 (2026-11-19, not yet ported back)

- Road input agnostic to the stage: `ue_x_cm/ue_y_cm` (UE handedness), `--frame`, `--units`, `--reverse`; a sparse road
  (median spacing > 2.5 m, e.g. a GPX) is curved through its points (centripetal Catmull-Rom) -- measured on Cruz
  decimated to 20 m: same grade 76/104, within one 101/104 (linear: 23 / 69; Gaussian smoothing worse).
- Note-system learning: grades 1..10 (the Finnish crew uses 1..9), number-first order detected per transcript
  (Concord v2), multi-word phrases (don't cut, keep in/out, over crest, very long), English slips from 137's FIX,
  accent folding (Finnish "pitkä"/"täysi" were cut to "pitk"/"t ysi" and never matched -- a 144 bug).
- Calibration: isotonic regression of log radius along the crew's speed order instead of a running max (which
  collapsed Finnish 1..5 to ACR 4); two passes (match, learn, re-match).
- Result: Whaanga's Finnish classes are monotone (rank -1.00: 46, 44, 42, 34, 31, 21, 20, 19, 12 m), 174/204 matched.
  Cruz: 55/58 matched (was 54); flat-out classes now come out ACR 4 instead of 5 (radius 37-53 m), severities differ
  from 144's output -- judge by ear before porting.

- Modifiers: union over all crews matched to a corner (144 kept only the first crew's). Tokens: the full
  `DT_PacenoteTokens_EA` (280 rows, ACR 0.6) is in `pacenotes-generator/acr_tokens.json`, read with acr-extrastages'
  `acr-ref export` from the vanilla view; `pacenotes.py` refuses at start-up any token not in it. Now produced: Flat,
  Tighten1..5, jumps / bumps / dips / crests by size, keep left/right/middle, brake, slippery, rocks, surfaces,
  bridge, junction. Concord: 9 of 16 heard "don't cut" reach the notes (the rest fall on kinks the finder skips).
- Note systems (Mountain + Loeb transcripts, 2026-11-19): Norwegian and French parsers; grade before the side with
  plus/minus or a conjunction in between ("6 minus right", "viis ja oikea"); SPEED grades ("droite 140", only with a
  distance unit in the transcript); grade words as classes ("nopea", "hidas"); plus/minus as their own classes.
- Calibration PER CREW (two onboards of one stage may count differently). Scale direction measured on NUMBER classes
  only, at >= 4 classes with |rank| >= 0.3 (words have a known direction: a Spanish run at -0.18 reversed its
  flat-out words; 3 classes gave an English crew "-1.00").
- Sync on side + spacing only (`fit_video`, sev_weight 0): scoring it on a guessed grade scale put a reversed Finnish
  crew one corner off. Cruz without its yaw sync now fits 16 s against the sync's 14 s, 57/58 matched.
- transcribe.py: `--language auto` over 6 windows (an English intro made Whisper translate a Finnish co-driver), GPU
  via pip CUDA wheels (`requirements-gpu.txt`, DLL dirs registered on Windows), UTF-8 console, `--clip` on a new file.

**The package is a work in progress** (owner's decision, 2026-11-19): every README says so at the top. Keep it that way until
he says otherwise.
