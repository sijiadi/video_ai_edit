"""transcribe_words.py — 用 faster-whisper(medium) 做词级转写，输出 JSON。

用法:
  python transcribe_words.py <out_dir> <model> <file1> [file2 ...]
输出: out_dir/<stem>.json  (words: [{text,start,end}], text, duration)
"""
from __future__ import annotations
import sys, os, json
from faster_whisper import WhisperModel


def main():
    out_dir = sys.argv[1]
    model_size = sys.argv[2]
    files = sys.argv[3:]
    os.makedirs(out_dir, exist_ok=True)
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    for f in files:
        print(f"[transcribe] {f} ...", flush=True)
        segs, _ = model.transcribe(
            f, language="zh", word_timestamps=True,
            vad_filter=True, vad_parameters=dict(min_silence_duration_ms=300))
        words = []
        for s in segs:
            for w in (s.words or []):
                t = w.word.strip()
                if t:
                    words.append({"text": t, "start": float(w.start), "end": float(w.end)})
        text = "".join(w["text"] for w in words)
        dur = words[-1]["end"] if words else 0.0
        stem = os.path.splitext(os.path.basename(f))[0]
        out = {"file": f, "words": words, "text": text, "duration": dur}
        op = os.path.join(out_dir, stem + ".json")
        with open(op, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)
        print(f"   -> {op}  ({len(words)} words, {dur:.2f}s)", flush=True)


if __name__ == "__main__":
    main()
