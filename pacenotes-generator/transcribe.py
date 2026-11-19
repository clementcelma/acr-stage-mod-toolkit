#!/usr/bin/env python3
r"""Onboard video -> co-driver transcript with word times (faster-whisper, local, nothing leaves the machine).

    python transcribe.py onboard.mp4 --language en --out transcript.json
    python transcribe.py onboard.mp4 --language es --model medium --out transcript.json
    python transcribe.py onboard.mp4 --language fi --out transcript.json --clip 232 328     (re-do one window)

The co-driver reads the notes over the intercom ("into left 5, 100, right 6 over crest, caution left four long"). This
step only TRANSCRIBES; pacenotes.py parses the calls and places them on the road.

Audio: ffmpeg (the one shipped with imageio-ffmpeg) -> 16 kHz mono, fed to faster-whisper as raw samples. No voice
activity filter: the engine is loud and a VAD drops whole calls. A pace-note prompt per language steers the vocabulary
(numbers, "over crest", "don't cut"). GPU (float16) when CUDA works, else CPU int8.

--clip T0 T1: without a VAD whisper can skip a whole window and return nothing for it; re-transcribe only those seconds
and splice them into the existing json.

Output: {"video", "model", "language", "duration_s", "segments": [{"start", "end", "text", "no_speech",
"words": [{"w", "t0", "t1", "p"}]}]} -- times in seconds of the video.
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

# a Windows console is cp1252: a Finnish or Norwegian word would crash a print, after hours of work
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# the co-driver's language picks the recognizer's vocabulary hint
PROMPTS = {
    "en": ("Rally pace notes: left 4, right 3 long, into, and, 100, 70, 50, 30, over crest, crest, jump, bump, dip, "
           "don't cut, cut, keep in, keep out, stay in, stay mid, caution, double caution, tightens, opens, long, very long, "
           "ruts, rough, narrows, bridge, junction, square, hairpin, flat, kink."),
    "es": ("Notas de rally: izquierda 4, derecha 3 larga, y, a, 100, 70, 50, 30, horquilla, escuadra, cerrada, abierta, "
           "rápida, larga, muy larga, cierra, abre, cresta, sobre cambio, cambio de rasante, salto, bache, no cortes, corta, "
           "cuidado, mucho cuidado, ojo, piano, por dentro, por fuera, estrecha, puente, cruce, plano, curva, recta, meta."),
    "fi": None,
}


def audio(path):
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    raw = subprocess.run([ff, "-v", "error", "-i", path, "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.int16).astype(np.float32) / 32768.0


def cuda_dlls():
    """Windows: the pip wheels nvidia-cublas-cu12 / nvidia-cudnn-cu12 put their DLLs in site-packages/nvidia/*/bin,
    which nothing searches. Register them before CTranslate2 loads, or the GPU fails with "cublas64_12.dll not found"."""
    if os.name != "nt":
        return
    import importlib.util
    for pkg in ("nvidia.cublas", "nvidia.cudnn", "nvidia.cuda_nvrtc", "nvidia.cuda_runtime"):
        try:
            spec = importlib.util.find_spec(pkg)
        except ModuleNotFoundError:
            continue
        for d in (spec.submodule_search_locations or []) if spec else []:
            b = os.path.join(d, "bin")
            if os.path.isdir(b):
                os.add_dll_directory(b)
                os.environ["PATH"] = b + os.pathsep + os.environ.get("PATH", "")


def load_model(name):
    cuda_dlls()
    from faster_whisper import WhisperModel
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return WhisperModel(name, device="cuda", compute_type="float16"), "cuda"
    except Exception:
        pass
    return WhisperModel(name, device="cpu", compute_type="int8"), "cpu"


def detect(m, x):
    """The co-driver's language from SIX windows across the run, not Whisper's first 30 s: onboards open with a
    narrator or a commentator. A Grönholm onboard with an English intro was detected "en", and Whisper then TRANSLATED
    the Finnish notes into English ("eight, left, fast")."""
    dur = len(x) / 16000
    votes = {}
    for f in (0.15, 0.3, 0.45, 0.6, 0.75, 0.9):
        t0 = int(max(0, dur * f - 15) * 16000)
        _, _, probs = m.detect_language(x[t0:t0 + 30 * 16000])
        for lg, p in probs:
            votes[lg] = votes.get(lg, 0.0) + p
    best = sorted(votes.items(), key=lambda kv: -kv[1])[:3]
    print("language over 6 windows: " + ", ".join("%s %.2f" % (lg, v / 6) for lg, v in best), flush=True)
    return best[0][0]


def run(m, x, a, offset=0.0):
    if a.language == "auto" and not getattr(a, "detected", None):
        a.detected = detect(m, x)
        if a.prompt is None:
            a.prompt = PROMPTS.get(a.detected)
    segs, info = m.transcribe(x, vad_filter=False, beam_size=5, word_timestamps=True,
                              language=a.detected if a.language == "auto" else a.language,
                              condition_on_previous_text=False, initial_prompt=a.prompt)
    a.detected = info.language
    return [{"start": round(s.start + offset, 2), "end": round(s.end + offset, 2), "text": s.text.strip(),
             "no_speech": round(s.no_speech_prob, 3),
             "words": [{"w": w.word.strip(), "t0": round(w.start + offset, 2), "t1": round(w.end + offset, 2),
                        "p": round(w.probability, 3)} for w in (s.words or [])]} for s in segs]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", help="onboard video or audio file (anything ffmpeg reads)")
    ap.add_argument("--out", required=True, help="transcript json")
    ap.add_argument("--language", default="en", help="the co-driver's language (en, es, fi, ... have parsers in "
                    "pacenotes.py); auto = let Whisper detect it (written to the json)")
    ap.add_argument("--model", default=None, help="faster-whisper model (default small.en for en, medium otherwise)")
    ap.add_argument("--prompt", default=None, help="vocabulary hint (default: the language's pace-note prompt)")
    ap.add_argument("--video-id", type=int, default=1, help="number written into the json (names the run in reports)")
    ap.add_argument("--clip", type=float, nargs=2, metavar=("T0", "T1"), default=None,
                    help="re-transcribe only seconds T0..T1 and splice them into the existing --out json")
    a = ap.parse_args()
    a.model = a.model or ("small.en" if a.language == "en" else "medium")
    a.prompt = a.prompt if a.prompt is not None else PROMPTS.get(a.language)
    if a.model.endswith(".en") and a.language != "en":
        raise SystemExit("model %s is English-only; the co-driver speaks %s" % (a.model, a.language))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    m, device = load_model(a.model)
    print("transcribe: %s, model %s on %s, language %s" % (a.video, a.model, device, a.language), flush=True)
    t0 = time.time()
    x = audio(a.video)
    try:
        if a.clip:
            c0, c1 = a.clip
            new = run(m, x[int(c0 * 16000):int(c1 * 16000)], a, offset=c0)
        else:
            new = run(m, x, a)
    except RuntimeError as e:                    # CUDA present but cuBLAS / cuDNN missing: redo on the CPU
        print("GPU failed (%s) -> CPU int8" % str(e)[:80], flush=True)
        from faster_whisper import WhisperModel
        m = WhisperModel(a.model, device="cpu", compute_type="int8")
        new = run(m, x[int(a.clip[0] * 16000):int(a.clip[1] * 16000)], a, offset=a.clip[0]) if a.clip else run(m, x, a)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    if a.clip:
        c0, c1 = a.clip
        old = json.load(open(a.out, encoding="utf-8")) if os.path.exists(a.out) else {
            "video": a.video_id, "model": a.model, "language": getattr(a, "detected", a.language),
            "duration_s": round(len(x) / 16000, 1), "segments": []}
        kept = [s for s in old["segments"] if s["end"] <= c0 or s["start"] >= c1]
        old["segments"] = sorted(kept + new, key=lambda s: s["start"])
        old.setdefault("clips", []).append([c0, c1, a.model])
        json.dump(old, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print("clip %.0f..%.0f s: %d segments spliced into %s" % (c0, c1, len(new), a.out))
        return
    for s in new:
        print("%7.1f  %s" % (s["start"], s["text"]))
    if a.language == "auto":
        print("detected language: %s" % a.detected)
    json.dump({"video": a.video_id, "model": a.model, "language": a.detected if a.language == "auto" else a.language, "duration_s": round(len(x) / 16000, 1),
               "segments": new}, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("%d segments, %d words, %.0f s audio in %.0f s -> %s"
          % (len(new), sum(len(s["words"]) for s in new), len(x) / 16000, time.time() - t0, a.out))


if __name__ == "__main__":
    main()
