#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def mlx_transcribe(input_path: Path, model: str) -> dict:
    import mlx_whisper

    return mlx_whisper.transcribe(
        str(input_path),
        path_or_hf_repo=model,
        language="ru",
        task="transcribe",
        word_timestamps=False,
        condition_on_previous_text=True,
        temperature=0,
        verbose=False,
    )


def faster_transcribe(input_path: Path, model: str) -> dict:
    from faster_whisper import WhisperModel

    device = "cuda" if os.environ.get("ROTHBALD_CUDA") == "1" else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"
    engine = WhisperModel(model, device=device, compute_type=compute_type, local_files_only=True)
    segments, info = engine.transcribe(
        str(input_path),
        language="ru",
        task="transcribe",
        beam_size=5,
        condition_on_previous_text=True,
        word_timestamps=False,
        vad_filter=True,
    )
    items = []
    duration = max(.001, float(getattr(info, "duration", 0) or 0))
    for segment in segments:
        items.append({"start": float(segment.start), "end": float(segment.end), "text": segment.text})
        print(f"{min(100, float(segment.end) / duration * 100):.1f}%", file=sys.stderr, flush=True)
    return {"text": " ".join(item["text"] for item in items), "language": "ru", "segments": items}


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit("usage: transcribe_video.py INPUT OUTPUT MODEL")
    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    model = sys.argv[3]

    result = faster_transcribe(input_path, model) if sys.platform == "win32" else mlx_transcribe(input_path, model)
    payload = {
        "text": result.get("text", ""),
        "language": result.get("language", "ru"),
        "segments": [
            {
                "start": float(segment.get("start", 0)),
                "end": float(segment.get("end", segment.get("start", 0))),
                "text": str(segment.get("text", "")).strip(),
            }
            for segment in result.get("segments", [])
        ],
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
