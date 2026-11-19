#!/usr/bin/env python3
r"""Co-driver transcripts + the road's centreline -> ACR pace notes (pacenotes.csv, pacenotes_acr.txt, report)

    python pacenotes.py --road road.csv transcript.json --out out/
    python pacenotes.py --road route.gpx t_v1.json t_v2.json --out out/        (several onboards of one stage)
    python pacenotes.py --road road.csv --out out/                             (geometry only, no onboard)
    python pacenotes.py --road road.csv t.json --sync t_sync.csv --out out/    (video time -> station known already)

ROAD: a CSV with s_m (optional) and utm_e/utm_n, x/y or e/n in metres, or lat/lon in degrees; or a GPX track. Start line
first, finish last; s 0 = the first point. A dense, smooth centreline (1-2 m) is what the corner finder expects -- a raw
GPS track works, but its wobble shows as extra small corners.

THE NUMBER SYSTEM. ACR's tables grade a corner 6 (fastest) .. 1 (slowest), then Square and HP (hairpin) -- the same
direction as the English/DR2 scale. The yardstick is ACR's own Hafren South table: median tightest radius over 10 m per
grade, HP 7, square 8, 1 14, 2 20, 3 26, 4 45, 5 65, 6 100 m.
A crew's own system is mapped onto it, never assumed to be it:
    - every call becomes a CLASS of the crew's vocabulary ("g4", "flat-", "limit", "hairpin" ...);
    - a class's ACR grade is read off the corners it was matched to (median min radius on Hafren's scale, >= 3 corners),
      so the crew's judgement survives and only the vocabulary changes;
    - the scale's DIRECTION is measured, not assumed: Finnish crews count 1 = fast .. 6 = slow (Rally NZ onboard: their
      "6" corners measure 21 m, their "1"s 46 m), and the classes are ordered on that measured direction;
    - Spanish is not a number scale at all: "fondo", "fondo menos", "apurar", a gear ("izquierda 2") ...
    - PHYSICS GUARD: a call 3+ grades faster than the radius allows is a slip of the match or the recognizer; the
      geometry's grade wins there. A 120+ deg turn under 12 m (or 160+ deg under 20 m) is always HP.

TWO SOURCES, ONE TABLE
    geometry   every corner of the road (heading change over 20 m, radius < 250 m, gaps < 10 m merged, >= 8 deg) -- the
               corner finder of acr-extrastages' stage-build, so each note lands 1:1 on the corner it will localise to.
    video      the co-driver's calls: their grade wins over the radius, and only they know crests, cautions,
               "tightens", "long". A call is matched to its corner by an order-preserving alignment (direction must
               agree, lead 0..250 m, grade close), after a sync of video time to distance: a rally car model on this
               road, with a start offset and a grip factor solved so the calls fall just before their corners. No
               landmark sync is needed -- the corner sequence IS the landmark.
    Corners an onboard does not cover, or the recognizer missed, keep the geometric grade.

A call is read 80 m before its first corner; corners closer than 40 m to the one before chain into one call
("Left4 Into Right3"), at most 3 per call.

OUTPUT (in --out)
    pacenotes.csv          one row per corner: call, subcall, read_s, start_s, end_s, direction, severity (ACR
                           1..6/square/hairpin), modifiers, distance_after_m, source, geometric_severity, min_radius_m,
                           turn_deg, said, acr_token
    pacenotes_acr.txt      the stage as ACR token rows (DT_PacenoteTokens_EA names): "s 1234  Left4 Tightens Into Right3
                           Dist100" -- a preview of what the table says; acr-extrastages' stage-build lays out the rows
    pacenotes_report.md    per onboard: every call heard, its class, its ACR grade, the corner it went to
"""
import argparse
import csv
import json
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

import numpy as np

import rally_pace

# a Windows console is cp1252: a Finnish or Norwegian word would crash a print, after hours of work
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# acr-extrastages config/pacenote-map.toml [layout] -- the corner finder must match stage-build's
WINDOW_M, CORNER_R_M, MERGE_M, MIN_TURN = 20.0, 250.0, 10.0, 8.0
LEAD_M, CHAIN_GAP_M, CHAIN_MAX = 80.0, 40.0, 3
# ACR's Hafren South table: median min radius (10 m window) per severity -- the geometric yardstick
HAFREN_R = {"hairpin": 7.0, "square": 8.0, "1": 14.0, "2": 20.0, "3": 26.0, "4": 45.0, "5": 65.0, "6": 100.0}
NUMERIC = ["1", "2", "3", "4", "5", "6"]
HAIRPIN_TURN, HAIRPIN_R = 120.0, 12.0        # deg, m (min radius over 10 m): a hairpin whatever the call ...
HAIRPIN_TURN2, HAIRPIN_R2 = 160.0, 20.0      # ... and so is any turn back on itself
CONTRADICT_STEPS = 3                         # a call this many severity steps faster than the radius is a slip

# ACR token names: rows of /Game/Data/Pacenote/Config/DT_PacenoteTokens_EA, listed in acr_tokens.json (280 tokens,
# every one but UpHill with English co-driver audio). Every name below is checked against that list at start-up, so a
# token that does not exist in the game can never be written.
ACR_DIR = {"left": "Left", "right": "Right"}
ACR_SEV = {"6": "{Dir}6", "5": "{Dir}5", "4": "{Dir}4", "3": "{Dir}3", "2": "{Dir}2", "1": "{Dir}1",
           "square": "{Dir}Square", "hairpin": "{Dir}HP", "flat": "{Dir}Flat",
           "acute_hairpin": "{Dir}AcuteHP", "open_hairpin": "{Dir}OpenHP"}
HAIRPINS = ("acute_hairpin", "hairpin", "open_hairpin")      # what the crew SAID; geometry only knows "hairpin"
ACR_MOD = {
    # turn details
    "long": "Long", "very_long": "VeryLong", "short": "Short", "tightens": "Tightens", "tightens_late": "TightensLate",
    "opens": "Opens", "opens_late": "OpensLate", "tighten_1": "Tighten1", "tighten_2": "Tighten2",
    "tighten_3": "Tighten3", "tighten_4": "Tighten4", "tighten_5": "Tighten5", "narrows": "Narrows", "widens": "Widens",
    # warnings
    "caution": "Caution", "dont_cut": "DontCut", "cut": "Cut", "small_cut": "SmallCut", "big_cut": "BigCut",
    "keep_in": "KeepIn", "keep_out": "KeepOut", "keep_left": "KeepLeft", "keep_right": "KeepRight",
    "keep_middle": "KeepMiddle", "brake": "Brake", "heavy_brake": "HeavyBrake", "handbrake": "Handbrake",
    "slippery": "Slippery", "hidden": "Hidden", "blind": "Blind", "sudden": "Sudden",
    # road details
    "crest": "Crest", "over_crest": "OverCrest", "small_crest": "SmallCrest", "big_crest": "BigCrest",
    "jump": "Jump", "over_jump": "OverJump", "small_jump": "JumpSmall", "big_jump": "JumpBig", "maybe_jump": "JumpMaybe",
    "bump": "Bump", "over_bump": "OverBump", "bumps": "Bumps", "dip": "Dip", "into_dip": "IntoDip", "big_dip": "BigDip",
    "rough": "Rough", "loose": "Loose", "ruts": "Ruts", "rocks": "Rocks", "ford": "Ford", "water": "WaterSplash",
    "ice": "Ice", "snow": "Snow", "tarmac": "OnAsphalt", "gravel": "OnGravel",
    # landmarks
    "bridge": "Bridge", "over_bridge": "OverBridge", "junction": "Junction",
}
UNMAPPED_MODS = ()


def _check_tokens():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acr_tokens.json")
    have = {t for ts in json.load(open(path, encoding="utf-8"))["categories"].values() for t in ts}
    want = [v.replace("{Dir}", d) for v in ACR_SEV.values() for d in ACR_DIR.values()] + list(ACR_MOD.values())
    missing = [t for t in want if t not in have]
    if missing:
        raise SystemExit("not ACR tokens (acr_tokens.json): %s" % missing)


_check_tokens()
ACR_DIST = [40, 50, 60, 70, 80, 90, 100, 130, 150, 170, 200, 250, 300, 350, 400]
STRAIGHT_MIN_M, LONG_STRAIGHT_M, INTO_MAX_M, AGREE_RATIO = 40.0, 500.0, 15.0, 1.5

# ---- the co-driver's words. A call = a SIDE word, then its description up to the next side word. Modifiers map to a
#      verified ACR token (the Hafren table uses each of them), except UNMAPPED_MODS (kept in the csv only).
#
# Spanish (Gran Canaria onboard) is NOT a 1-6 scale: it is the speed-descriptor system --
#   "derecha fondo" flat right; "fondo buena" / "fondo menos" / "fondo menos menos" / "mala": a step slower each;
#   "apurar" on the limit; a gear on the slow ones ("frenar ... izquierda 2"); "cierra" / "tarde cierra" tightens /
#   tightens late; "ojo" caution; "rasante" crest; "larga" long; "100" the distance to the next.
# The recognizer's slips are the measured ones (medium model): forco / forno / forto / foto -> fondo, sierra / cierre ->
# cierra, razante / rasarte -> rasante.
LANG = {
    "es": {
        "left": {"izquierda", "izquierdo", "izda"}, "right": {"derecha", "derecho", "dcha"},
        "num": {"uno": "1", "una": "1", "primera": "1", "dos": "2", "segunda": "2", "tres": "3", "tercera": "3",
                "cuatro": "4", "cuarta": "4", "cinco": "5", "quinta": "5", "seis": "6", "sexta": "6"},
        "fix": {"forco": "fondo", "forno": "fondo", "forto": "fondo", "foto": "fondo", "fonda": "fondo",
                "sierra": "cierra", "cierre": "cierra", "cierran": "cierra", "razante": "rasante",
                "rasarte": "rasante", "rasantes": "rasante", "horquilla": "hairpin", "orquilla": "hairpin",
                "escuadra": "square", "cien": "100", "ciento": "100", "cincuenta": "50", "setenta": "70",
                "doscientos": "200", "trescientos": "300", "treinta": "30"},
        "flat": "fondo", "limit": "apurar", "minus": "menos", "plus": "buena", "bad": "mala", "almost": "casi",
        "mods": {"cierra": "tightens", "abre": "opens", "ojo": "caution", "cuidado": "caution", "larga": "long",
                 "rasante": "crest", "cambio": "crest", "no_cortes": "dont_cut", "muy_larga": "very_long",
                 "salto": "jump", "bache": "bump", "baches": "bumps", "corta": "short", "frenar": "brake",
                 "frena": "brake", "estrecha": "narrows", "puente": "bridge", "cruce": "junction", "piedras": "rocks",
                 "piedra": "rocks", "roderas": "ruts", "hielo": "ice", "nieve": "snow", "asfalto": "tarmac",
                 "tierra": "gravel", "por_dentro": "keep_in", "por_fuera": "keep_out", "abre_tarde": "opens_late",
                 "agua": "water", "vado": "ford", "ciega": "blind", "resbala": "slippery"},
        "phrases": [(("no", "cortes"), "no_cortes"), (("no", "corte"), "no_cortes"), (("muy", "larga"), "muy_larga"),
                    (("por", "dentro"), "por_dentro"), (("por", "fuera"), "por_fuera"), (("abre", "tarde"), "abre_tarde")],
        "late": "tarde", "finish": "meta",
    },
    # Finnish (Rally NZ 2010 onboard, medium model). Recognizer slips seen: "masen" / "ase" -> vasen,
    # "oikein" / "oikee" -> oikea, "kuolkyt" -> kolkyt. Heard on that onboard: jarru (brake), alä kiviä (rocks),
    # liukas (slippery), yli, pitkä, varo. NOT "jää" (ice): folded to "jaa" it matched other words at call ends.
    "fi": {
        "left": {"vasen", "vasemmalle", "vasempaan", "masen", "vase"}, "right": {"oikea", "oikee", "oikein",
                                                                                 "oikeaan", "oikealle"},
        "num": {"yksi": "1", "yks": "1", "ykkönen": "1", "kaksi": "2", "kaks": "2", "kakkonen": "2", "kolme": "3",
                "kolmonen": "3", "neljä": "4", "nelonen": "4", "viisi": "5", "viis": "5", "vitonen": "5",
                "kuusi": "6", "kuus": "6", "kutonen": "6", "seitseman": "7", "seittema": "7", "seiska": "7",
                "kahdeksan": "8", "kasi": "8", "yhdeksan": "9", "ysi": "9", "kymmenen": "10", "kymppi": "10"},
        "conj": {"ja"},
        # Grönholm's crew (2007): speed words beside or instead of numbers. nopea fast, helppo easy, hidas slow
        "grade_words": {"nopea": 5.0, "nopee": 5.0, "helppo": 5.5, "hidas": 2.0, "tosi_hidas": 1.0},
        "fix": {"kuolkyt": "30", "kolkyt": "30", "kolmekymmentä": "30", "kakskyt": "20", "kaksikymmentä": "20",
                "nelkyt": "40", "viiskyt": "50", "viisikymmentä": "50", "seitkyt": "70", "sata": "100",
                "satanen": "100", "kaksisataa": "200", "kakssataa": "200", "nyppy": "yli", "nypyn": "yli",
                "nypky": "yli", "harja": "yli", "hiukka": "hiukan"},
        "flat": {"täysi", "täys"}, "limit": None, "minus": "hiukan", "plus": None, "bad": None, "almost": None,
        "mods": {"tiukkenee": "tightens", "kiristyy": "tightens", "avautuu": "opens", "aukee": "opens",
                 "auki": "opens", "varo": "caution", "pitkä": "long", "yli": "crest", "hyppy": "jump",
                 "hyppyyn": "jump", "lyhyt": "short", "jarru": "brake", "jarruta": "brake", "kapenee": "narrows",
                 "silta": "bridge", "risteys": "junction", "kivi": "rocks", "kivia": "rocks", "urat": "ruts",
                 "asfaltti": "tarmac", "sora": "gravel", "ala_oikaise": "dont_cut", "liukas": "slippery",
                 "kumpare": "bump", "notko": "dip", "sokea": "blind", "kirraa": "tightens", "tiraa": "tightens",
                 "huomio": "caution", "ahdas": "narrows", "ahdasta": "narrows"},
        "phrases": [(("ala", "oikaise"), "ala_oikaise"), (("tosi", "hidas"), "tosi_hidas")],
        "late": "myöhään", "finish": "maali",
    },
    # Norwegian (Rally Norway 2009 SS10 onboard, H. Solberg's co-driver; medium model). Number FIRST ("kort 6 venstre
    # pluss over kroen, 40"), pluss / min, flat, distances bare. "kroen" is the recognizer's spelling of the crew's
    # crest word (it falls where every other crew says crest); the real word is not verified.
    "no": {
        "left": {"venstre", "ventre", "vendstre"}, "right": {"høyre", "hoyre", "høire"},
        "num": {"en": "1", "ett": "1", "to": "2", "tre": "3", "fire": "4", "fem": "5", "seks": "6", "sju": "7",
                "syv": "7", "åtte": "8", "ni": "9", "ti": "10"},
        "conj": {"og", "til"},
        "fix": {"hundre": "100", "femti": "50", "seksti": "60", "sytti": "70", "åtti": "80", "førti": "40",
                "tretti": "30", "hallang": "halvlang", "flett": "flatt", "krøen": "kroen", "krøe": "kroen"},
        "flat": {"flat", "flatt"}, "limit": None, "minus": "min", "plus": "pluss", "bad": None, "almost": None,
        "mods": {"kort": "short", "lang": "long", "åpner": "opens", "strammer": "tightens", "stammer": "tightens",
                 "kroen": "crest", "over_kroen": "over_crest", "lite_kroen": "small_crest", "hopp": "jump",
                 "over_hopp": "over_jump", "ikke_kutt": "dont_cut", "kutt": "cut", "hold_inn": "keep_in",
                 "hold_ut": "keep_out", "stein": "rocks", "steiner": "rocks", "brems": "brake", "obs": "caution",
                 "forsiktig": "caution", "bro": "bridge", "bru": "bridge", "humper": "bumps", "dump": "dip"},
        "phrases": [(("over", "kroen"), "over_kroen"), (("lite", "kroen"), "lite_kroen"), (("over", "hopp"), "over_hopp"),
                    (("ikke", "kutt"), "ikke_kutt"), (("hold", "inn"), "hold_inn"), (("hold", "ut"), "hold_ut")],
        "late": "sen", "finish": "mål",
    },
    # French (Loeb's co-driver, Rally Norway 2009 SS23; medium model). SPEED grades: "droite 140 plus", "gauche 60
    # plus epingle"; distances WITH "metres"; "a fond" flat. Parsed from that onboard only: not matched to a road yet.
    "fr": {
        "left": {"gauche", "gauchy"}, "right": {"droite", "droit", "droits", "droites"},
        "num": {"un": "1", "deux": "2", "trois": "3", "quatre": "4", "cinq": "5", "six": "6", "sept": "7",
                "huit": "8", "neuf": "9", "dix": "10", "cent": "100", "cinquante": "50", "soixante": "60",
                "quatre_vingt": "80", "trente": "30", "quarante": "40"},
        "conj": {"et"},
        "units": {"metres", "metre", "m"},
        "fix": {"gauchafond": "gauche", "millons": "moins", "milot": "mi_long", "epingle": "hairpin",
                "epingles": "hairpin", "equerre": "square"},
        "flat": "fond", "limit": None, "minus": "moins", "plus": "plus", "bad": None, "almost": None,
        "mods": {"attention": "caution", "frein": "brake", "freine": "brake", "ferme": "tightens", "ouvre": "opens",
                 "long": "long", "tres_long": "very_long", "court": "short", "bosse": "bump", "bosses": "bumps", "saut": "jump",
                 "cuvette": "dip", "pont": "bridge", "ne_coupe_pas": "dont_cut", "coupe": "cut"},
        "phrases": [(("tres", "long"), "tres_long"), (("ne", "coupe", "pas"), "ne_coupe_pas"),
                    (("coupe", "pas"), "ne_coupe_pas"), (("quatre", "vingt"), "quatre_vingt")],
        "late": "tard", "finish": "arrivee",
    },
    # English. Recognizer slips measured on US onboards (small.en): press / crass -> crest, titans / tying -> tightens,
    # rate / red -> right; "over crest" heard as one word.
    "en": {
        "left": {"left"}, "right": {"right", "rate", "red"},
        "num": {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10"},
        "fix": {"hundred": "100", "fifty": "50", "seventy": "70", "thirty": "30", "forty": "40", "sixty": "60",
                "eighty": "80", "press": "crest", "pressed": "crest", "presses": "crest", "crass": "crest",
                "titans": "tightens", "titan": "tightens", "tying": "tightens", "tighten": "tightens",
                "overcrest": "over_crest", "overcrests": "over_crest", "hairpins": "hairpin"},
        "flat": "flat", "limit": None, "minus": "minus", "plus": "plus", "bad": None, "almost": None,
        "conj": {"and"},
        "mods": {"brow": "crest", "over_brow": "over_crest", "big_brow": "big_crest", "hump": "bump",
                 "humps": "bumps", "care": "caution", "pinch": "narrows", "pinches": "narrows", "dont": "dont_cut",
                 "tightens": "tightens", "opens": "opens", "caution": "caution", "long": "long", "crest": "crest",
                 "over_crest": "over_crest", "dont_cut": "dont_cut", "keep_in": "keep_in", "keep_out": "keep_out",
                 "very_long": "very_long", "jump": "jump", "jumps": "jump", "bump": "bump", "bumps": "bumps",
                 "dip": "dip", "compression": "dip", "short": "short", "cut": "cut", "small_cut": "small_cut",
                 "keep_left": "keep_left", "keep_right": "keep_right", "keep_middle": "keep_middle",
                 "narrows": "narrows", "widens": "widens", "brake": "brake", "heavy_brake": "heavy_brake",
                 "handbrake": "handbrake", "slippery": "slippery", "rough": "rough", "loose": "loose", "ruts": "ruts",
                 "rocks": "rocks", "rock": "rocks", "ford": "ford", "splash": "water", "water": "water",
                 "bridge": "bridge", "over_bridge": "over_bridge", "junction": "junction", "ice": "ice",
                 "icy": "ice", "snow": "snow", "tarmac": "tarmac", "asphalt": "tarmac", "gravel": "gravel",
                 "hidden": "hidden", "blind": "blind", "opens_late": "opens_late", "small_crest": "small_crest",
                 "big_crest": "big_crest", "over_jump": "over_jump", "small_jump": "small_jump",
                 "big_jump": "big_jump", "maybe_jump": "maybe_jump", "over_bump": "over_bump",
                 "into_dip": "into_dip", "big_dip": "big_dip"},
        "phrases": [(("don", "t", "cut"), "dont_cut"), (("dont", "cut"), "dont_cut"), (("no", "cut"), "dont_cut"),
                    (("keep", "in"), "keep_in"), (("stay", "in"), "keep_in"), (("keep", "out"), "keep_out"),
                    (("stay", "out"), "keep_out"), (("over", "crest"), "over_crest"), (("over", "the", "rest"), "over_crest"),
                    (("keep", "left"), "keep_left"), (("stay", "left"), "keep_left"), (("keep", "right"), "keep_right"),
                    (("stay", "right"), "keep_right"), (("keep", "middle"), "keep_middle"), (("stay", "mid"), "keep_middle"),
                    (("stay", "middle"), "keep_middle"), (("small", "cut"), "small_cut"), (("heavy", "brake"), "heavy_brake"),
                    (("hand", "brake"), "handbrake"), (("opens", "late"), "opens_late"), (("small", "crest"), "small_crest"),
                    (("big", "crest"), "big_crest"), (("over", "jump"), "over_jump"), (("small", "jump"), "small_jump"),
                    (("big", "jump"), "big_jump"), (("maybe", "jump"), "maybe_jump"), (("over", "bump"), "over_bump"),
                    (("into", "dip"), "into_dip"), (("big", "dip"), "big_dip"), (("over", "bridge"), "over_bridge"),
                    (("water", "splash"), "water"), (("over", "brow"), "over_brow"), (("big", "brow"), "big_brow"),
                    (("acute", "hairpin"), "acute_hairpin"), (("open", "hairpin"), "open_hairpin"),
                    (("very", "long"), "very_long")],
        "late": "late", "finish": "finish",
    },
}
GRADE_MAX = 10          # crews grade 1..6, 1..7, 1..10 ...: any small number right next to the side word is its grade
CREST_MODS = ("crest", "over_crest")
FLIP_MIN_RS = 0.3       # a crew's number scale is taken as reversed only on a rank correlation this clear
# the heuristic speed of a class before it is calibrated (alignment only): ACR-like 1 (slow) .. 6 (flat)
FLAT_SCORE, LIMIT_SCORE, MINUS_STEP = 6.0, 4.0, 0.75


# ---------------------------------------------------------------- the road
def read_road(path, frame="auto", units="m"):
    """(s, east, north) in metres from a CSV or a GPX track.

    Only left/right matters, so the frame is about handedness: GIS (x = east, y = north, seen from above) or UE (X
    forward, Y right, Z up -- left-handed). Columns named ue_x* / ue_y* are UE; x / y follow --frame (default GIS).
    Getting this wrong mirrors every corner."""
    if path.lower().endswith(".gpx"):
        root = ET.parse(path).getroot()
        pts = [(float(p.get("lat")), float(p.get("lon"))) for p in root.iter() if p.tag.split("}")[-1] in ("trkpt", "rtept")]
        lat, lon = np.array(pts).T
        s = None
    else:
        rows = [r for r in csv.DictReader(l for l in open(path, encoding="utf-8-sig") if not l.startswith("#"))]
        cols = {k.lower().strip(): k for k in rows[0]}
        get = lambda name: np.array([float(r[cols[name]]) for r in rows])
        s = get("s_m") if "s_m" in cols else None
        for cx, cy, scale in (("ue_x_cm", "ue_y_cm", 0.01), ("ue_x", "ue_y", 0.01), ("x_cm", "y_cm", 0.01)):
            if cx in cols and cy in cols:                 # UE: X -> Y is a turn to the RIGHT seen from above
                x, y = get(cx) * scale, get(cy) * scale
                return _stations(s, y, x) if (frame != "gis" and cx.startswith("ue")) or frame == "ue" else _stations(s, x, y)
        for ce, cn in (("utm_e", "utm_n"), ("x", "y"), ("e", "n"), ("east", "north"), ("easting", "northing")):
            if ce in cols and cn in cols:
                k = 0.01 if units == "cm" else 1.0
                x, y = get(ce) * k, get(cn) * k
                return _stations(s, y, x) if frame == "ue" else _stations(s, x, y)
        lat_c = next((c for c in ("lat", "latitude") if c in cols), None)
        lon_c = next((c for c in ("lon", "lng", "longitude") if c in cols), None)
        if not (lat_c and lon_c):
            raise SystemExit("road csv %s: no ue_x_cm/ue_y_cm, utm_e/utm_n, x/y, e/n or lat/lon columns (has %s)"
                             % (path, list(rows[0])))
        lat, lon = get(lat_c), get(lon_c)
    # degrees -> local metres (equirectangular about the mean latitude: exact enough for curvature over a stage)
    R = 6371008.8
    e = np.radians(lon - lon.mean()) * R * math.cos(math.radians(lat.mean()))
    n = np.radians(lat - lat.mean()) * R
    return _stations(s, e, n)


def _stations(s, e, n):
    if s is None:
        s = np.r_[0.0, np.cumsum(np.hypot(np.diff(e), np.diff(n)))]
    keep = np.r_[True, np.diff(s) > 1e-6]                 # repeated points (GPS standing still)
    return s[keep], e[keep], n[keep]


def catmull_rom(e, n, alpha=0.5, step=0.5):
    """Centripetal Catmull-Rom through sparse points (a GPX every 10-30 m). Linear interpolation puts every turn in a
    kink at a point and reads it as a hairpin; on a 20 m GPX of a measured road this curve gives the same grade on 76
    of 104 corners and within one grade on 101 (linear: 23 and 69; a Gaussian smoothing does worse still)."""
    p = np.c_[e, n]
    p = np.vstack([2 * p[0] - p[1], p, 2 * p[-1] - p[-2]])
    out = []
    for i in range(1, len(p) - 2):
        p0, p1, p2, p3 = p[i - 1], p[i], p[i + 1], p[i + 2]
        t0 = 0.0
        t1 = t0 + max(np.linalg.norm(p1 - p0), 1e-6) ** alpha
        t2 = t1 + max(np.linalg.norm(p2 - p1), 1e-6) ** alpha
        t3 = t2 + max(np.linalg.norm(p3 - p2), 1e-6) ** alpha
        for t in np.linspace(t1, t2, max(2, int(np.linalg.norm(p2 - p1) / step)), endpoint=False):
            a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
            a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
            a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
            b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
            b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
            out.append((t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2)
    out.append(p[-2])
    out = np.array(out)
    return out[:, 0], out[:, 1]


SPARSE_M = 2.5                                   # median point spacing above which the road is curved through its points


class Road:
    def __init__(self, path, frame="auto", units="m", reverse=False):
        s, e, n = read_road(path, frame, units)
        if reverse:                                       # a track drawn finish -> start
            e, n, s = e[::-1], n[::-1], None
        self.spacing = float(np.median(np.diff(s))) if s is not None and len(s) > 1 else float(
            np.median(np.hypot(np.diff(e), np.diff(n))))
        if self.spacing > SPARSE_M:
            e, n = catmull_rom(e, n)
            s = None
        if s is None:
            s, e, n = _stations(None, e, n)
        self.S = np.arange(0.0, s[-1], 1.0)
        self.E, self.N = np.interp(self.S, s, e), np.interp(self.S, s, n)
        # compass heading (clockwise from north): increasing = turning RIGHT
        h = np.arctan2(np.diff(self.E), np.diff(self.N))
        self.H = np.unwrap(np.r_[h, h[-1]])
        self.length = float(self.S[-1])

    def heading(self, s):
        return np.interp(s, self.S, self.H)

    def K(self, s, w=10.0):
        return (self.heading(s + w / 2) - self.heading(s - w / 2)) / w

    def turn(self, a, b):
        return math.degrees(float(self.heading(b) - self.heading(a)))

    def min_radius(self, a, b, w=10.0):
        ss = np.arange(a, b + 1e-9, 2.0)
        dh = np.abs(self.heading(ss + w / 2) - self.heading(ss - w / 2))
        dh = dh[dh > 1e-6]
        return float(w / dh.max()) if len(dh) else 9999.0

    def corners(self):
        kmin, step = 1.0 / CORNER_R_M, 2.0
        runs = []
        for s in np.arange(WINDOW_M / 2, self.length - WINDOW_M / 2, step):
            k = float(self.K(s, WINDOW_M))
            sign = 0 if abs(k) < kmin else (1 if k > 0 else -1)
            if not sign:
                continue
            if runs and runs[-1][2] == sign and s - runs[-1][1] <= MERGE_M + step:
                runs[-1][1] = s
            else:
                runs.append([s, s, sign])
        out = []
        for a, b, sign in runs:
            b = max(b, a + step)
            t = self.turn(a, b)
            if abs(t) >= MIN_TURN:
                out.append({"start": float(a), "end": float(b), "turn": t, "dir": "right" if t > 0 else "left",
                            "min_r": self.min_radius(a, b)})
        return out


# ---------------------------------------------------------------- severity from radius
def scale_from(medians):
    """Boundaries between severities: geometric means of neighbouring median radii (numeric 1..6)."""
    m = [medians[k] for k in NUMERIC]
    return [math.sqrt(m[i] * m[i + 1]) for i in range(5)]


def is_hairpin(c):
    t, r = abs(c["turn"]), c["min_r"]
    return (t >= HAIRPIN_TURN and r < HAIRPIN_R) or (t >= HAIRPIN_TURN2 and r < HAIRPIN_R2)


def geom_severity(c, medians):
    r, t = c["min_r"], abs(c["turn"])
    tight = math.sqrt(medians["1"] * min(medians["hairpin"], medians["square"]))
    if is_hairpin(c):
        return "hairpin"
    if r < tight and t >= 70:
        return "square"
    b = scale_from(medians)
    for i, edge in enumerate(b):
        if r < edge:
            return NUMERIC[i]
    return "6"


def sev_rank(sev):
    named = {"acute_hairpin": -0.25, "hairpin": 0.0, "open_hairpin": 0.25, "square": 0.5, "flat": 6.5}
    return named[sev] if sev in named else float(sev)


def severity_of_radius(r, medians):
    for i, edge in enumerate(scale_from(medians)):
        if r < edge:
            return NUMERIC[i]
    return "6"


# ---------------------------------------------------------------- the co-driver's calls
FOLD = (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ñ", "n"), ("ä", "a"), ("ö", "o"), ("å", "a"),
        ("à", "a"), ("è", "e"), ("ì", "i"), ("ò", "o"), ("ù", "u"), ("ü", "u"), ("ç", "c"), ("â", "a"), ("ê", "e"),
        ("î", "i"), ("ô", "o"), ("û", "u"), ("ø", "o"), ("æ", "ae"), ("ë", "e"), ("ï", "i"))


def fold(w):
    """Lower case, accents off: the recognizer's spelling and the vocabulary's meet here (Finnish "pitkä" was being
    cut to "pitk" and never matched)."""
    w = w.lower()
    for a, b in FOLD:
        w = w.replace(a, b)
    return w


def _fold_lang(L):
    f = lambda x: fold(x) if isinstance(x, str) else x
    out = {}
    for k, v in L.items():
        if isinstance(v, set):
            out[k] = {fold(x) for x in v}
        elif isinstance(v, dict):
            out[k] = {fold(a): f(b) for a, b in v.items()}
        elif k == "phrases":
            out[k] = [(tuple(fold(x) for x in ws), t) for ws, t in v]
        else:
            out[k] = f(v)
    return out


LANG = {k: _fold_lang(v) for k, v in LANG.items()}


def norm_word(w, L):
    w = fold(w)
    out = []
    for p in re.sub(r"[^a-z0-9 ]", " ", w.replace("-", " ")).split():
        p = L["num"].get(p, p)
        out.append(L["fix"].get(p, p))
    return out


def merge_phrases(toks, L):
    """Multi-word calls ("don't cut", "keep in", "over crest") -> one token, at the first word's time."""
    phrases = L.get("phrases", [])
    out, i = [], 0
    while i < len(toks):
        for words, tok in phrases:
            if tuple(p for _, p in toks[i:i + len(words)]) == words:
                out.append((toks[i][0], tok))
                i += len(words)
                break
        else:
            out.append(toks[i])
            i += 1
    return out


def _is(L, key, p):
    v = L.get(key)
    return v is not None and (p in v if isinstance(v, (set, frozenset)) else p == v)


def is_grade(p, scale="small"):
    if not p.isdigit():
        return False
    n = int(p)
    return 20 <= n <= 250 if scale == "speed" else 1 <= n <= GRADE_MAX


def grade_scale(toks, sides, L):
    """How this crew grades, read off the transcript: "small" (1..6, 1..10 ...) or "speed" -- the corner's speed,
    30..250 ("droite 140 plus", Loeb's co-driver), with distances said WITH a unit ("130 metres"). Needs the unit
    words to be there: without them a big number after a side word is a distance."""
    near = [int(toks[j][1]) for i in sides for j in (i - 1, i + 1) if 0 <= j < len(toks) and toks[j][1].isdigit()]
    big = sum(1 for n in near if 30 <= n <= 250)
    small = sum(1 for n in near if 1 <= n <= GRADE_MAX)
    units = sum(1 for _, p in toks if p in L.get("units", ()))
    return "speed" if big > 2 * small and big >= 8 and units >= 3 else "small"


def grade_before(toks, i, lo, L, scale, sides_set):
    """The grade said BEFORE side word i ("4 left", "6 minus right", "viis ja oikea"): (index, plus/minus/conj words
    between), or None. A grade right after another side word belongs to that one ("vasen 3 ja oikea")."""
    skip = {w for w in (L.get("plus"), L.get("minus")) if w} | set(L.get("conj", ()))
    j, between = i - 1, []
    while j >= lo and len(between) < 2 and toks[j][1] in skip:
        between.insert(0, toks[j][1])
        j -= 1
    if j >= lo and is_grade(toks[j][1], scale) and (j - 1 not in sides_set):
        return j, between
    return None


def number_first(toks, sides, L, scale):
    """Does this crew say "4 left" rather than "left 4"? Counted on the transcript itself, not assumed per language."""
    ss = set(sides)
    after = sum(1 for i in sides if i + 1 < len(toks) and is_grade(toks[i + 1][1], scale))
    before = sum(1 for i in sides if grade_before(toks, i, 0, L, scale, ss))
    return before > 1.5 * after and before >= 5


def parse_calls(transcript, L):
    """Corner calls in time order: {t, dir, cls, score, sev (hairpin/square or None), mods, dist_after, said}.
    A call's time is its side word's own timestamp.

    A call is the words from the end of the previous call (its distance) to the next call: what is said before the
    side word ("over crest, flat right", "short 6 minus right") belongs to it as much as what follows."""
    toks = [(w["t0"], p) for seg in transcript["segments"] for w in seg["words"] for p in norm_word(w["w"], L)]
    toks = merge_phrases(toks, L)
    sides = [i for i, (_, p) in enumerate(toks) if p in L["left"] or p in L["right"]]
    ss = set(sides)
    scale = grade_scale(toks, sides, L)
    nf = number_first(toks, sides, L, scale)
    units = set(L.get("units", ()))
    calls, prev_end = [], 0
    for k, i in enumerate(sides):
        nxt = sides[k + 1] if k + 1 < len(sides) else len(toks)
        if nf and nxt < len(toks):
            gb = grade_before(toks, nxt, i + 1, L, scale, ss)
            if gb:
                nxt = gb[0]                               # the next call's grade, said before its side word
        start, pre = i, []
        if nf:
            gb = grade_before(toks, i, prev_end, L, scale, ss)
            if gb:
                start, pre = gb[0], [toks[gb[0]][1]] + gb[1]
        before = [p for _, p in toks[max(prev_end, start - 4):start]]
        d = "left" if toks[i][1] in L["left"] else "right"
        # the description ends at the call's distance: what follows it belongs to the next call
        desc, dist, end, got = [], None, nxt, False
        for idx in range(i + 1, nxt):
            p = toks[idx][1]
            if p in units:
                continue
            if p.isdigit():
                n, unit = int(p), idx + 1 < len(toks) and toks[idx + 1][1] in units
                if scale == "speed" and not unit and not got and not nf and is_grade(p, scale):
                    desc.append(p)                        # "droite 140": the grade
                    got = True
                    continue
                if (scale == "speed" and (unit or got or nf)) or (scale == "small" and n >= 30):
                    dist, end = n, idx + 1
                    break
            desc.append(p)
        prev_end = end
        desc = pre + desc
        words = before + desc
        gear = next((p for p in desc[:3] if is_grade(p, scale)), None)
        sev = next((h for h in HAIRPINS if h in desc[:4]), None) or ("square" if "square" in desc[:4] else None)
        # minus steps that grade the corner, not the ones that grade its tightening ("cierra menos")
        minus = sum(1 for j, p in enumerate(desc) if _is(L, "minus", p) and not (j and desc[j - 1] in ("cierra", L["minus"]) and "cierra" in desc[:j]))
        plus_n = sum(1 for p in desc if _is(L, "plus", p))
        flat, limit = any(_is(L, "flat", p) for p in words), any(_is(L, "limit", p) for p in desc)
        plus, bad, almost = plus_n > 0, any(_is(L, "bad", p) for p in desc), any(_is(L, "almost", p) for p in desc)
        if sev:
            cls, score = sev, 0.0
        elif gear:
            # "5 plus" / "6 minus": a third of a step either way; the calibration decides whether it is a distinct grade
            cls = "g" + gear + ("~" if almost else "") + "+" * min(plus_n, 2) + "-" * min(minus, 2)
            score = float(gear) + (plus_n - minus) / 3.0
        elif flat:
            cls = "flat" + ("+" if plus else "") + "-" * min(minus, 3) + ("!" if bad else "")
            score = FLAT_SCORE + (0.5 if plus else 0) - MINUS_STEP * min(minus, 3) - (0.5 if bad else 0)
            if limit:
                cls, score = "limit " + cls, score - 0.5
        elif limit:
            cls, score = "limit" + "-" * min(minus, 3), LIMIT_SCORE - MINUS_STEP * min(minus, 3)
        elif any(p in L.get("grade_words", {}) for p in desc[:4]):
            # a crew grading with words ("vasen nopea", "oikea hidas"): each word is a class, its value only a first
            # guess for the alignment; the calibration reads its real grade off the corners
            w = next(p for p in desc[:4] if p in L.get("grade_words", {}))
            cls, score = w + "+" * min(plus_n, 2) + "-" * min(minus, 2), L["grade_words"][w] + (plus_n - minus) / 3.0
        else:
            cls, score = "?", None
        mods = []
        for p in words:
            mm = L["mods"].get(p)
            if mm and mm not in mods:
                mods.append(mm)
        if "tightens" in mods and any(desc[j] == L["late"] and L["mods"].get(desc[j + 1]) == "tightens"
                                      for j in range(len(desc) - 1)):
            mods[mods.index("tightens")] = "tightens_late"
        # "tightens 3": the grade it tightens TO, on the crew's own scale (mapped to ACR once the scale is learnt)
        tighten_to = next((desc[j + 1] for j in range(len(desc) - 1)
                           if L["mods"].get(desc[j]) == "tightens" and is_grade(desc[j + 1], scale)), None)
        calls.append({"t": toks[i][0], "dir": d, "cls": cls, "score": score, "sev": sev, "mods": mods,
                      "tighten_to": tighten_to, "dist_after": dist,
                      "said": " ".join(before + [p for _, p in toks[start:end]])[:70]})
    # a crew grading 1..7, 1..10 or by speed: bring the heuristic speed onto 1..6 for the first alignment only (the
    # calibration then learns the real mapping)
    gs = [c for c in calls if c["cls"].startswith("g")]
    if gs:
        lo, hi = min(c["score"] for c in gs), max(c["score"] for c in gs)
        if hi > 6.7:
            for c in gs:
                c["score"] = 1.0 + 5.0 * (c["score"] - lo) / max(hi - lo, 1e-6)
    for c in calls:
        c["number_first"], c["scale"] = nf, scale
    return calls


def isotonic(values, weights):
    """Pool-adjacent-violators: the non-decreasing sequence closest to values (weighted least squares)."""
    blocks = [[v, w, 1] for v, w in zip(values, weights)]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0]:
            (v1, w1, n1), (v2, w2, n2) = blocks[i], blocks[i + 1]
            blocks[i:i + 2] = [[(v1 * w1 + v2 * w2) / (w1 + w2), w1 + w2, n1 + n2]]
            i = max(i - 1, 0)
        else:
            i += 1
    return [v for v, _, n in blocks for _ in range(n)]


def calibrate(crew, corners, label=None):
    """The crew's vocabulary -> ACR severity, learnt from the corners each class was matched to.

    Per class the median min radius of its corners; the classes are put in the crew's speed order -- whose DIRECTION
    is measured (rank correlation of class number against radius: Finnish crews count 1 = fast), never assumed --
    and the radii are made monotone along it by isotonic regression (log radius, weighted by corners), so a noisy
    class is pulled towards its neighbours instead of dragging every faster class down to its grade. The pooled
    radius is then read on Hafren's scale. Hairpin / square calls stay what they say."""
    classes = {}
    for i, (v, c) in crew.items():
        classes.setdefault(c["cls"], {"r": [], "score": c["score"], "sev": c["sev"]})["r"].append(corners[i]["min_r"])
    cls_sev = {k: d["sev"] for k, d in classes.items() if d["sev"]}
    graded = [k for k, d in classes.items() if d["score"] is not None and not d["sev"]]
    # Only NUMBERS can run either way; a crew's words ("fondo menos", "apurar", "hidas") have a known direction. So
    # the direction is measured on the number classes alone, and flips only them, and only on a clear correlation
    # (2026-11-19: a Spanish run at -0.18 reversed its flat-out words when words and numbers were tested together)
    numeric = [k for k in graded if re.match(r"g\d", k)]
    flip = False
    big = [k for k in numeric if len(classes[k]["r"]) >= 3]
    # >= 4 classes: with 3 a rank correlation can only be +-1 or +-0.5, and an English crew on a road of kinks came
    # out "reversed" at -1.00 on 3 classes (Concord v8)
    if len(big) >= 4:
        sc = np.array([classes[k]["score"] for k in big], float)
        mr = np.array([np.median(classes[k]["r"]) for k in big], float)
        rs = np.corrcoef(np.argsort(np.argsort(sc)), np.argsort(np.argsort(mr)))[0, 1]
        flip = rs <= -FLIP_MIN_RS
        if label is not None:
            print("  v%s number scale vs measured radius: rank correlation %.2f -> %s"
                  % (label, rs, "REVERSED (higher number = tighter)" if flip else "higher number = faster"
                     if rs >= FLIP_MIN_RS else "unclear, kept higher = faster"))
    if flip:                                              # mirror the numbers within their own range
        lo, hi = min(classes[k]["score"] for k in numeric), max(classes[k]["score"] for k in numeric)
        for k in numeric:
            classes[k]["score"] = lo + hi - classes[k]["score"]
    ordered = sorted(graded, key=lambda k: classes[k]["score"])
    pooled = isotonic([math.log(float(np.median(classes[k]["r"]))) for k in ordered],
                      [len(classes[k]["r"]) for k in ordered])
    for k, lr in zip(ordered, pooled):
        cls_sev[k] = severity_of_radius(math.exp(lr), HAFREN_R)
        classes[k]["pooled"] = math.exp(lr)
    return classes, cls_sev, flip


def call_rank(c):
    """The call's speed on the 0 (hairpin) .. 6 (flat) scale, for alignment: the calibrated severity once known."""
    if c.get("acr_sev"):
        return sev_rank(c["acr_sev"])
    if c["sev"]:
        return sev_rank(c["sev"])
    return c["score"]


# ---------------------------------------------------------------- sync: video time -> station
def pace_time(road, grip):
    """Seconds from the start line to every 2 m station, rally_pace car, standing start."""
    xy = list(zip(road.E[::2], road.N[::2]))
    kap = rally_pace.menger_curvature(xy, 5)
    v = rally_pace.profile(kap, 2.0, grip=grip, v_in=0.0)
    floor = rally_pace.MIN_KMH / 3.6
    t = np.r_[0.0, np.cumsum([2.0 / max(0.5 * (v[i] + v[i + 1]), floor) for i in range(len(v) - 1)])]
    return road.S[::2], t


def align(calls, corners, call_s, medians, sev_weight=0.25):
    """Order-preserving: call j -> corner i (direction equal, lead = corner start - call station in [-30, 250]).
    Cost: lead outside 20..150 m, severity distance; skipping a call 2, skipping a corner 0.3 (geometry fills it)."""
    n, k = len(calls), len(corners)
    INF = 1e18
    dp = np.full((n + 1, k + 1), INF)
    bk = np.zeros((n + 1, k + 1, 2), int)
    dp[0, :] = np.arange(k + 1) * 0.3
    for j in range(n + 1):
        for i in range(k + 1):
            cur = dp[j, i]
            if cur >= INF:
                continue
            if j < n and cur + 2.0 < dp[j + 1, i]:
                dp[j + 1, i], bk[j + 1, i] = cur + 2.0, (j, i)
            if i < k and cur + 0.3 < dp[j, i + 1]:
                dp[j, i + 1], bk[j, i + 1] = cur + 0.3, (j, i)
            if j < n and i < k:
                c, p = corners[i], calls[j]
                if c["dir"] != p["dir"]:
                    continue
                lead = c["start"] - call_s[j]
                if not (-30 <= lead <= 250):
                    continue
                cost = (max(0, 20 - lead) + max(0, lead - 150)) / 50.0
                rc = call_rank(p) if sev_weight else None
                if rc is not None:
                    cost += sev_weight * abs(sev_rank(geom_severity(c, medians)) - rc)
                if cur + cost < dp[j + 1, i + 1]:
                    dp[j + 1, i + 1], bk[j + 1, i + 1] = cur + cost, (j, i)
    j, i = n, k
    pairs = []
    while j > 0 or i > 0:
        pj, pi = bk[j, i]
        if pj == j - 1 and pi == i - 1:
            pairs.append((pj, pi))
        j, i = pj, pi
    return float(dp[n, k]), pairs[::-1]


def read_sync(path):
    """(t, s) from a CSV with t_s, s_m columns: video seconds -> metres from the start line."""
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    return np.array([float(r["t_s"]) for r in rows]), np.array([float(r["s_m"]) for r in rows])


def fit_video(calls, corners, road, medians):
    """Grid over grip and start offset; the best alignment wins. Returns (offset, grip, pairs, cost, median lead).

    The sync uses side and spacing ONLY (sev_weight 0): the crew's grades are not known yet -- their scale may run
    either way -- and scoring the sync on a guessed scale put a reversed Finnish crew one corner off (start -4 s
    instead of 24 s). The order of lefts and rights is the landmark; grades join the match after calibration."""
    best = None
    def search(grips, offs):
        nonlocal best
        for grip in grips:
            S2, T = pace_time(road, grip)
            for off in offs:
                call_s = np.interp([c["t"] - off for c in calls], T, S2, left=-1e4, right=1e6)
                cost, pairs = align(calls, corners, call_s, medians, sev_weight=0.0)
                score = cost - 1.0 * len(pairs)
                if best is None or score < best[0]:
                    best = (score, off, grip, pairs, cost)
    search(np.arange(0.8, 2.61, 0.2), np.arange(-20.0, 90.01, 3.0))          # coarse
    g0, o0 = best[2], best[1]
    search(np.arange(g0 - 0.15, g0 + 0.151, 0.05), np.arange(o0 - 3, o0 + 3.01, 0.5))  # fine, around the coarse best
    _, off, grip, pairs, cost = best
    S2, T = pace_time(road, grip)
    call_s = np.interp([c["t"] - off for c in calls], T, S2)
    leads = [corners[i]["start"] - call_s[j] for j, i in pairs]
    return off, grip, pairs, cost, float(np.median(leads)) if leads else None


# ---------------------------------------------------------------- ACR tokens
def tighten_mods(mods, call, cls_sev):
    """"tightens 3" -> Tighten<N> with N on ACR's scale: the crew's grade read through its learnt class (g3 -> ACR ?)."""
    raw = call.get("tighten_to")
    if not raw or "tightens" not in mods:
        return mods
    n = cls_sev.get("g" + raw, raw if int(raw) <= 6 else None)
    if n not in ("1", "2", "3", "4", "5"):
        return mods                                       # ACR has Tighten1..5 only
    return [("tighten_" + n) if m == "tightens" else m for m in mods]


def acr_corner(direction, severity, modifiers):
    d = ACR_DIR[direction]
    if "over_crest" in modifiers:                         # two crews, "crest" and "over crest": one call
        modifiers = [m for m in modifiers if m != "crest"]
    return " ".join([ACR_SEV[severity].replace("{Dir}", d)] + [ACR_MOD[m] for m in modifiers if m in ACR_MOD])


def acr_dist(m):
    if m >= LONG_STRAIGHT_M:
        return "LongStraight"
    return "Dist%d" % min(ACR_DIST, key=lambda v: abs(v - m))


def acr_lines(rows, road_length):
    """One line per call: the corner tokens chained with Into / And, then the DistNN of the straight after it."""
    calls = {}
    for r in rows:
        calls.setdefault(r["call"], []).append(r)
    order = sorted(calls)
    out = []
    for k, c in enumerate(order):
        rs = calls[c]
        toks = []
        for j, r in enumerate(rs):
            if j:
                toks.append("Into" if r["start_s"] - rs[j - 1]["end_s"] < INTO_MAX_M else "And")
            toks.append(r["acr_token"])
        last = rs[-1]
        gap = (calls[order[k + 1]][0]["start_s"] if k + 1 < len(order) else road_length) - last["end_s"]
        if gap >= STRAIGHT_MIN_M:
            said = last["distance_after_m"]
            # the crew's distance when the geometry agrees with it, else the geometry's
            use = float(said) if said and gap / AGREE_RATIO <= float(said) <= gap * AGREE_RATIO else gap
            toks.append(acr_dist(use))
        out.append("s %6.0f  %s" % (rs[0]["read_s"], " ".join(toks)))
    out.append("s %6.0f  Finish" % max(0.0, road_length - 50.0))
    return out


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("transcripts", nargs="*", help="transcribe.py json, one per onboard of this stage (none = geometry only)")
    ap.add_argument("--road", required=True, help="the stage's centre spline as points: CSV (ue_x_cm/ue_y_cm, "
                    "utm_e/utm_n, x/y, e/n or lat/lon; s_m optional), or a GPX track as a fallback")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--sync", nargs="*", default=[], help="per transcript, in the same order: CSV t_s,s_m (video time -> "
                    "metres from the start line); '-' = fit it from the calls (the default)")
    ap.add_argument("--language", default=None, help="override the transcript's language (en, es, fi)")
    ap.add_argument("--dry", action="store_true", help="fit and report, write nothing")
    ap.add_argument("--frame", choices=["auto", "gis", "ue"], default="auto",
                    help="x/y handedness: gis = x east, y north; ue = Unreal X forward, Y right (auto: ue_x*/ue_y* "
                         "columns are UE, anything else GIS). A wrong frame mirrors every corner")
    ap.add_argument("--units", choices=["m", "cm"], default="m", help="units of x/y columns (ue_*_cm / *_cm are cm anyway)")
    ap.add_argument("--reverse", action="store_true", help="the road file runs finish -> start")
    a = ap.parse_args()
    road = Road(a.road, a.frame, a.units, a.reverse)
    corners = road.corners()
    print("pacenotes: road %s (%.1f m, a point every %.1f m%s), %d geometric corners (%d left, %d right)"
          % (a.road, road.length, road.spacing, " -> curved through the points: geometric grades approximate"
             if road.spacing > SPARSE_M else "", len(corners), sum(c["dir"] == "left" for c in corners),
             sum(c["dir"] == "right" for c in corners)))

    medians = dict(HAFREN_R)
    trs = [json.load(open(tp, encoding="utf-8")) for tp in a.transcripts]
    ids = [tr.get("video") for tr in trs]
    labels = [str(i) for i in ids] if None not in ids and len(set(ids)) == len(ids) else \
        [os.path.splitext(os.path.basename(tp))[0] for tp in a.transcripts]
    runs = []                                             # (label, calls, call_s, off, grip)
    for idx, (tp, tr) in enumerate(zip(a.transcripts, trs)):
        v = labels[idx]
        lang = a.language or tr.get("language") or tr.get("lang") or "en"
        if lang not in LANG:
            raise SystemExit("%s: no parser for language %r (have %s) -- add one to LANG" % (tp, lang, sorted(LANG)))
        calls = parse_calls(tr, LANG[lang])
        if len(calls) < 8:
            print("  v%s: only %d corner calls recognised -- not used" % (v, len(calls)))
            continue
        sync = a.sync[idx] if idx < len(a.sync) and a.sync[idx] != "-" else None
        if sync:
            tt, ss = read_sync(sync)
            call_s = np.interp([c["t"] for c in calls], tt, ss, left=-1e4, right=1e6)
            off, grip, how = float(tt[0]), 0.0, "synced by %s" % os.path.basename(sync)
        else:
            off, grip, _, _, _ = fit_video(calls, corners, road, medians)
            S2, T = pace_time(road, grip)
            call_s = np.interp([c["t"] - off for c in calls], T, S2, left=-1e4, right=1e6)
            how = "own fit"
        print("  v%s (%s, %s): %d corner calls, %s first; start at video %.0f s, %s"
              % (v, lang, tr.get("model"), len(calls), "number" if calls[0]["number_first"] else "side", off, how))
        runs.append((v, calls, call_s, off, grip))

    # ---- two passes: match with the heuristic grades, learn the crew's system from the matches, then match again
    #      with the learnt grades (a reversed or 1..10 scale costs the first alignment, not the second)
    for rnd in (1, 2):
        crew, crew_mods, report, own = {}, {}, [], {}
        for v, calls, call_s, off, grip in runs:
            cost, pairs = align(calls, corners, call_s, medians)
            own[v] = {i: (v, calls[j]) for j, i in pairs}
            for j, i in pairs:
                # several onboards on one corner: the grade from the first crew that graded it, the modifiers of all
                # ("don't cut" said by one crew of four is still true)
                if i not in crew or (crew[i][1]["cls"] == "?" and calls[j]["cls"] != "?"):
                    crew[i] = (v, calls[j])
                mm = crew_mods.setdefault(i, [])
                mm += [m for m in calls[j]["mods"] if m not in mm]
            report.append((v, calls, pairs, off, grip))
            if rnd == 2:
                leads = [corners[i]["start"] - call_s[j] for j, i in pairs]
                print("  v%s: %d of %d calls matched to corners (%.0f %%), median lead %s m"
                      % (v, len(pairs), len(calls), 100.0 * len(pairs) / len(calls),
                         "%.0f" % float(np.median(leads)) if leads else "-"))
        # each crew's system is learnt on its OWN matches: two crews of one stage may count differently (Mountain:
        # a Norwegian 1..6 crew and a Finnish crew grading 2..9 and with words)
        run_sev, run_classes = {}, {}
        for v, calls, *_ in runs:
            run_classes[v], run_sev[v], _ = calibrate(own[v], corners, v if rnd == 2 else None) if own[v] else ({}, {}, False)
            for c in calls:
                c["acr_sev"] = run_sev[v].get(c["cls"])
    for v, *_ in runs:
        classes, cls_sev = run_classes[v], run_sev[v]
        if classes:
            print("  v%s classes -> ACR severity (corners, median min radius -> monotone radius):" % v)
            for k in sorted(classes, key=lambda k: (classes[k]["score"] is None, classes[k]["score"] or 0)):
                d = classes[k]
                print("    %-14s -> %-8s (%2d, %5.0f m -> %s)" % (k, cls_sev.get(k, "geometry"), len(d["r"]),
                                                              float(np.median(d["r"])),
                                                              "%.0f m" % d["pooled"] if "pooled" in d else "-"))

    # ---- one note per corner, chained into calls
    notes = []
    for i, c in enumerate(corners):
        g = geom_severity(c, medians)
        hairpin = is_hairpin(c)
        if i in crew and crew[i][1].get("acr_sev"):
            v, p = crew[i]
            sev, src = p["acr_sev"], "v%s %s" % (v, p["cls"])
            if sev_rank(sev) - sev_rank(g) >= CONTRADICT_STEPS:
                # PHYSICS GUARD: a call 3+ steps faster than the radius allows is a slip of the match or the recognizer
                # (a "fondo" heard on a 5.8 m / 213 deg hairpin), not the crew's judgement
                sev, src = g, src + " CONTRADICTS radius -> geometry"
            elif hairpin and sev not in HAIRPINS:         # a 2nd-gear call round 120+ deg at < 12 m is ACR's hairpin
                sev = "hairpin"
            elif p["cls"] == "flat" and sev == "6":        # the crew said flat and it measures a 6: ACR's {Dir}Flat
                sev = "flat"
            notes.append(dict(c, sev=sev, mods=tighten_mods(crew_mods[i], p, run_sev.get(v, {})), source=src, said=p["said"], geom=g,
                              dist_after=p["dist_after"]))
        elif i in crew:                                   # the crew's side and modifiers, geometry's grade
            v, p = crew[i]
            notes.append(dict(c, sev=g, mods=crew_mods[i], source="v%s ? + geometry" % v, said=p["said"], geom=g,
                              dist_after=p["dist_after"]))
        else:
            notes.append(dict(c, sev=g, mods=[], source="geometry", said="", geom=g, dist_after=None))
    call, sub, rows = 0, 0, []
    for k, nt in enumerate(notes):
        if k == 0 or nt["start"] - notes[k - 1]["end"] >= CHAIN_GAP_M or sub >= CHAIN_MAX - 1:
            call += 1
            sub = 0
            read = max(0.0, nt["start"] - LEAD_M)
        else:
            sub += 1
        rows.append({"call": call, "subcall": sub, "read_s": round(read, 1), "start_s": round(nt["start"], 1),
                     "end_s": round(nt["end"], 1), "direction": nt["dir"], "severity": nt["sev"],
                     "modifiers": "|".join(nt["mods"]), "distance_after_m": nt["dist_after"] or "",
                     "source": nt["source"], "geometric_severity": nt["geom"], "min_radius_m": round(nt["min_r"], 1),
                     "turn_deg": round(nt["turn"], 1), "said": nt["said"],
                     "acr_token": acr_corner(nt["dir"], nt["sev"], nt["mods"])})
    n_crew = sum(r["source"] != "geometry" for r in rows)
    agree = sum(r["source"] != "geometry" and r["severity"] == r["geometric_severity"] for r in rows)
    print("  notes: %d corners in %d calls; %d from the crew (%d with the same severity as the geometry), %d from geometry"
          % (len(rows), call, n_crew, agree, len(rows) - n_crew))
    sev_count = {}
    for r in rows:
        sev_count[r["severity"]] = sev_count.get(r["severity"], 0) + 1
    print("  severities: " + ", ".join("%s %d" % (k, sev_count[k]) for k in ["hairpin", "square"] + NUMERIC if k in sev_count))

    lines = ["# Pace notes: %s" % os.path.basename(a.road), "",
             "Written by pacenotes.py. %d corners, %d calls; %d severities from the crew, %d from geometry." %
             (len(rows), call, n_crew, len(rows) - n_crew), "",
             "Scale (median min radius, m): " + ", ".join("%s %.0f" % (k, medians[k]) for k in ["hairpin", "square"] + NUMERIC), ""]
    for v, calls, pairs, off, grip in report:
        matched = {j for j, _ in pairs}
        lines += ["## Video %s: %d calls, %d matched (start at video %.0f s, grip %.1f)" % (v, len(calls), len(pairs), off, grip), "",
                  "| video t | said | class | ACR | modifiers | corner s | radius m | turn | geometry says |",
                  "|---:|---|---|---|---|---:|---:|---:|---|"]
        cmap = dict(pairs)
        for j, c in enumerate(calls):
            head = "| %d:%04.1f | %s | %s | %s | %s |" % (c["t"] // 60, c["t"] % 60, c["said"], c["cls"],
                                                        c.get("acr_sev") or "-", " ".join(c["mods"]))
            if j in matched:
                k = corners[cmap[j]]
                lines.append(head + " %.0f | %.0f | %.0f | %s |" % (k["start"], k["min_r"], k["turn"], geom_severity(k, medians)))
            else:
                lines.append(head + " **unmatched** | | | |")
        lines.append("")
    acr = acr_lines(rows, road.length)
    if a.dry:
        print("\n".join(acr[:12]) + "\n  ...\n--dry: nothing written")
        return
    os.makedirs(a.out, exist_ok=True)
    out = os.path.join(a.out, "pacenotes.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    open(os.path.join(a.out, "pacenotes_acr.txt"), "w", encoding="utf-8").write(
        "# read point (m from the start line) and the ACR tokens of the call\n" + "\n".join(acr) + "\n")
    open(os.path.join(a.out, "pacenotes_report.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("wrote %s, pacenotes_acr.txt, pacenotes_report.md" % out)


if __name__ == "__main__":
    main()
