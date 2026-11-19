# pacenotes-generator

> **Work in progress.** This tool is tested on three stages and three co-driver languages. The grades it produces are a first pass to be checked by ear in game, not a finished pace-note set. Expect the parsers, the matching and the output format to change.
>
> **Based on Assetto Corsa Rally 0.6 (Early Access).** The grading scale (Hafren South table) and the token list (`acr_tokens.json`) were read from ACR 0.6. After a game update, re-read `DT_PacenoteTokens_EA` before trusting the tokens.

Turns an onboard video of a real rally stage into pace notes for Assetto Corsa Rally (ACR), graded on ACR's own scale.

It works in two steps:

1. **`transcribe.py`** listens to the co-driver on the intercom and writes down every word with its time (faster-whisper; runs locally, nothing is uploaded).
2. **`pacenotes.py`** finds every corner of your stage's road, matches each call the co-driver read to the corner it describes, learns the crew's note system from those matches, and writes the notes as ACR tokens (`Left4 Tightens Into Right3 Dist100`).

Nothing in it is tied to a particular stage or video: the inputs are the road and the transcripts. Without a video, `pacenotes.py` still grades every corner from the road's geometry.

## Install

Python 3.10+.

```
pip install -r requirements.txt
```

The first run of `transcribe.py` downloads the Whisper model (`small.en` about 0.5 GB, `medium` about 1.5 GB). **CUDA is not required.** An NVIDIA GPU is used when one works; otherwise it runs on the CPU, which is slower but gives the same result. With an NVIDIA card, also install CUDA 12's libraries (about 1 GB; no CUDA install needed, `transcribe.py` finds them itself):

```
pip install -r requirements.txt -r requirements-gpu.txt
```

## Use

```
python transcribe.py onboard.mp4 --language en --out work/onboard1.json
python pacenotes.py --road road.csv work/onboard1.json --out work/notes
```

Several onboards of the same stage, each one transcribed to its own json:

```
python pacenotes.py --road road.csv work/onboard1.json work/onboard2.json --out work/notes
```

Geometry only, with no video:

```
python pacenotes.py --road road.csv --out work/notes
```

### The road: use your stage's centre spline

The notes must land on the road the player drives, and ACR's `stage-build` places them on the corners of that spline. So `--road` should be **the centre spline of your stage, exported as points**: a CSV with one row per point, start line first and finish last.

| Columns | Read as |
|---|---|
| `ue_x_cm, ue_y_cm` | Unreal coordinates in cm (X forward, Y right) |
| `utm_e, utm_n` / `x, y` / `e, n` | metres, east and north (add `--units cm` for centimetres, `--frame ue` if your x/y are Unreal axes) |
| `lat, lon` | degrees |
| `s_m` (optional) | distance along the road; computed when absent |

A wrong frame mirrors every corner, so a left is called right. If the report shows almost every call unmatched, try the other `--frame`. Use `--reverse` if the file runs from finish to start.

**A GPX track is a fallback.** Points more than 2.5 m apart are joined by a smooth curve (centripetal Catmull-Rom) before the corners are found. Measured against the dense road of a 9 km stage:

| Road input | Same grade as the dense road | Within one grade |
|---|---|---|
| GPX, a point every 10 m | 88 / 104 corners | 104 / 104 |
| GPX, every 20 m (rally-maps) | 76 / 104 | 101 / 104 |
| noisy GPS log, every 30 m | 29 / 104 | 64 / 104 |

Corners the co-driver called keep the crew's grade whatever the road input; only the corners graded from geometry suffer from a rough line.

### Languages

`--language auto` lets Whisper find the co-driver's language; it samples six windows across the video, because onboards often open with a narrator (an English intro made Whisper *translate* a Finnish co-driver into English). Parsers exist for:

| | Seen on | How that crew calls a corner |
|---|---|---|
| `en` | 5 US onboards | "left 4 tightens, 100" and, for one crew, "short 6 minus right" |
| `fi` | Rally NZ 2010, Rally Norway 2007 | "vasen 5 pitkä" (1..9, 1 = fast) / "seitsemän, ja vasen nopea" (number first, speed words) |
| `es` | Gran Canaria | speed words: "derecha fondo menos", "izquierda apurar cierra" |
| `no` | Rally Norway 2009 | "kort 6 venstre pluss over krøen, 40" (number first, plus/minus) |
| `fr` | Rally Norway 2009 (SS23) | **speeds**: "droite 140 plus", "gauche 60 épingle", distances in "mètres" (parsed; not yet matched to a road) |

To add a language, add an entry to `LANG` in `pacenotes.py`: side words, number words, plus/minus, conjunctions, modifiers, multi-word phrases, the recogniser's typical mistakes, and grade words if the crew grades with words. Optionally add a vocabulary prompt to `PROMPTS` in `transcribe.py`.

### Whisper missed a stretch

With no voice-activity filter (the engine noise makes one drop calls), Whisper sometimes returns nothing for a whole window. Re-transcribe just those seconds and splice them into the existing file:

```
python transcribe.py onboard.mp4 --language es --out work/onboard1.json --clip 232 328
```

### Already know where the car is?

If you have a video-time-to-distance table (a CSV with `t_s,s_m` columns), pass it with `--sync`, one per transcript in the same order (`-` means fit that one from the calls). Without it, the sync is fitted from the calls themselves (see below).

## The number system

ACR grades a corner **6 (fastest) down to 1 (slowest)**, then **Square** and **HP** (hairpin). The yardstick is ACR's own Hafren South table, taking the tightest radius over 10 m:

| HP | Square | 1 | 2 | 3 | 4 | 5 | 6 |
|---|---|---|---|---|---|---|---|
| 7 m | 8 m | 14 m | 20 m | 26 m | 45 m | 65 m | 100 m |

Co-drivers do not all use that system, and no particular system is assumed. Each crew's system is **learnt from its own calls**:

- **Order of words:** "left 4", "4 left", "6 minus right" or "viis, ja oikea" (with a word in between) is counted on the transcript itself.
- **Range:** any grade from 1 to 10 is read, not just 1 to 6 (one Finnish crew used 1 to 9). A crew calling corners by their **speed** ("droite 140") is recognised when its distances carry a unit ("130 mètres").
- **Plus and minus:** "5 plus", "6 minus" are their own classes; the calibration decides whether they differ from a plain 5 or 6.
- **Grade words:** a crew grading with words ("vasen nopea" = left fast, "hidas" = slow) gets one class per word.
- **Each crew on its own:** two onboards of the same stage are calibrated separately, because two crews may count differently.
- **Direction:** measured, as the rank correlation between the crew's number and the radius of the corners it was matched to. The Finnish crew counts 1 = fastest; their grades 1 to 9 measure 46, 44, 42, 34, 31, 21, 20, 19 and 12 m (correlation −1.00).
- **Words instead of numbers:** Spanish crews grade by speed words ("fondo", "fondo menos", "apurar", a gear for slow corners). Each wording becomes a class and is placed on the same scale.
- **Mapping:** each class gets the median radius of its corners. The classes are put in the crew's speed order, and the radii are made monotone along it (isotonic regression, weighted by the number of corners), so one noisy class cannot drag its neighbours down. The result is read on the Hafren scale.
- **Two passes:** the calls are matched to corners once with rough grades, the crew's system is learnt from those matches, and then the calls are matched again with the learnt grades.
- **Physics guard:** a call three or more grades faster than the radius allows is a recognition or matching slip, and the geometry's grade wins. A turn of 120° or more under 12 m (or 160° or more under 20 m) is always HP.

### Modifiers and ACR tokens

Every token the generator writes is checked at start-up against **`acr_tokens.json`**: the 280 row names of the game's `DT_PacenoteTokens_EA` (all of them, by category; every one except `UpHill` has English co-driver audio in the game). A token that is not in the game can never be written.

When several onboards cover one corner, the modifiers of all of them are kept: a "don't cut" said by one crew of four is still true.

| Group | ACR tokens produced |
|---|---|
| Grades | `Left1`..`Left6`, `LeftSquare`, `LeftHP`, `LeftAcuteHP` / `LeftOpenHP` (when the crew says so), `LeftFlat` (the crew said "flat" and it measures a 6), same for Right |
| Turn | `Long`, `VeryLong`, `Short`, `Tightens`, `TightensLate`, `Tighten1`..`Tighten5` ("tightens 3", the 3 read on the crew's learnt scale), `Opens`, `OpensLate`, `Narrows`, `Widens` |
| Warnings | `Caution`, `DontCut`, `Cut`, `SmallCut`, `KeepIn`, `KeepOut`, `KeepLeft`, `KeepRight`, `KeepMiddle`, `Brake`, `HeavyBrake`, `Handbrake`, `Slippery`, `Hidden`, `Blind` |
| Road | `Crest`, `OverCrest`, `SmallCrest`, `BigCrest`, `Jump`, `OverJump`, `JumpSmall`, `JumpBig`, `JumpMaybe`, `Bump`, `OverBump`, `Bumps`, `Dip`, `IntoDip`, `BigDip`, `Rough`, `Loose`, `Ruts`, `Rocks`, `Ford`, `WaterSplash`, `Ice`, `Snow`, `OnAsphalt`, `OnGravel` |
| Landmarks | `Bridge`, `OverBridge`, `Junction` |
| Layout | `Into`, `And`, `Dist40`..`Dist400`, `LongStraight`, `Finish` |

English has words for all of them. Spanish and Finnish cover the grades and the common calls (tightens, opens, long, crest, caution, brake, rocks, jump, don't cut ...); words not yet heard on an onboard are marked in `LANG`.

Not produced yet, although ACR has them: kinks (`LeftKink`, `Kinks`), chicanes, `GoLeft` / `GoRight` / `GoStraight`, and the landmark calls (houses, gates, poles, signs ...).

## How a call finds its corner

The corner finder is the one used by [acr-extrastages](https://github.com/nitnerolf420/acr-extrastages)' `stage-build`: heading change over 20 m, radius under 250 m, gaps under 10 m merged, at least 8°. Each note therefore lands on the corner `stage-build` will put it on.

To know where the car was when a call was read, a simple rally-car model (`rally_pace.py`: grip, braking, power, drag) drives the road. A start offset and a grip factor are searched until the calls fall just before their corners. The calls are then matched to the corners by an order-preserving alignment: the direction must agree, the corner must be 0 to 250 m ahead, and the grades must be close. No landmarks are needed; the sequence of corners is the landmark.

A call is read 80 m before its first corner. Corners less than 40 m apart are chained into one call (`Left4 Into Right3`, or `And` when the gap is over 15 m), with at most three per call.

## Output

| File | Contents |
|---|---|
| `pacenotes.csv` | One row per corner: `call`, `subcall`, `read_s`, `start_s`, `end_s`, `direction`, `severity` (ACR `1`..`6` / `square` / `hairpin`), `modifiers`, `distance_after_m`, `source` (which onboard and class, or `geometry`), `geometric_severity`, `min_radius_m`, `turn_deg`, `said`, `acr_token` |
| `pacenotes_acr.txt` | The stage as ACR token rows, for example `s 1712  Right4 Opens Crest Dist350`. The names are rows of `DT_PacenoteTokens_EA`. It is a preview: `stage-build` lays out the final table rows. |
| `pacenotes_report.md` | For each onboard, every call heard, its class, its ACR grade and the corner it was matched to (or **unmatched**). Read this first when a note sounds wrong. |

## Known limits

- Not every onboard has notes: of 15 videos transcribed, 5 had none usable (a documentary, a YouTube voice-over, a run with chat only, two with the intercom inaudible under the engine). Whisper then invents text, which shows in the report as calls with no grade.
- On a road of fast kinks (New England), only about half the calls match a corner: kinks are not corners for the corner finder.
- The calibrated grades are only as good as the matches. Check `pacenotes_report.md` and listen in game.
- A crew calling flat out ("fondo", "flat") on corners of 40 to 50 m radius gets ACR 4, because the radius is what ACR's scale measures.
- Distances: a crew's "100" is used when the geometry agrees with it within a factor of 1.5; otherwise the measured straight is used.
- Five languages have parsers; French is parsed but not yet tested against a road.
- A modifier is only kept when its call was matched to a corner. On a road of fast kinks (New England), many "don't cut" calls fall on kinks the corner finder skips: 9 of the 16 heard reach the notes.
- "Short" is ambiguous in English notes (a short corner, or a short distance to the next). It is read as the corner modifier.
- Kinks (turns under 8°) are not corners for `stage-build`'s finder, so a "kink" call has nowhere to go yet.
