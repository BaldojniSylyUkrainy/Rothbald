#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def resolve_faster_whisper_device(preference: str, cuda_count: int) -> tuple[str, int, str]:
    preference = preference.lower()
    if preference.startswith("cuda:"):
        try:
            device_index = int(preference.split(":", 1)[1])
        except ValueError as exc:
            raise RuntimeError("Некоректно збережений вибір NVIDIA GPU") from exc
        if device_index < 0 or device_index >= cuda_count:
            raise RuntimeError("Обрана NVIDIA GPU більше недоступна. Вибери Auto або CPU у Rothbald.")
        return "cuda", device_index, "float16"
    if preference == "auto" and cuda_count:
        return "cuda", 0, "float16"
    return "cpu", 0, "int8"


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
    import ctranslate2
    from faster_whisper import WhisperModel

    preference = os.environ.get("ROTHBALD_DEVICE", "auto").lower()
    cuda_count = max(0, int(ctranslate2.get_cuda_device_count()))
    device, device_index, compute_type = resolve_faster_whisper_device(preference, cuda_count)
    engine = WhisperModel(
        model,
        device=device,
        device_index=device_index,
        compute_type=compute_type,
        local_files_only=True,
    )
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
