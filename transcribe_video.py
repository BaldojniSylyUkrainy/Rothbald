#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from hardware_check import runtime_tool_path
from model_manager import WINDOWS_VULKAN_WHISPER_PATTERNS, WINDOWS_VULKAN_WHISPER_REPO
from process_utils import quiet_process_options


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


def whisper_language(language_mode: str) -> str | None:
    if language_mode == "standard":
        return "ru"
    if language_mode == "auto":
        return None
    raise RuntimeError("Невідомий режим мови розпізнавання")


def mlx_transcribe(input_path: Path, model: str, language_mode: str = "standard") -> dict:
    import mlx_whisper

    return mlx_whisper.transcribe(
        str(input_path),
        path_or_hf_repo=model,
        language=whisper_language(language_mode),
        task="transcribe",
        word_timestamps=False,
        condition_on_previous_text=True,
        temperature=0,
        verbose=False,
    )


def faster_transcribe(input_path: Path, model: str, language_mode: str = "standard") -> dict:
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
        language=whisper_language(language_mode),
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
    detected = str(getattr(info, "language", "") or whisper_language(language_mode) or "")
    return {"text": " ".join(item["text"] for item in items), "language": detected, "segments": items}


def parse_whisper_cpp_result(payload: dict) -> dict:
    segments = []
    for item in payload.get("transcription", []):
        if not isinstance(item, dict):
            continue
        offsets = item.get("offsets", {})
        try:
            start = float(offsets.get("from", 0)) / 1000
            end = float(offsets.get("to", offsets.get("from", 0))) / 1000
        except (AttributeError, TypeError, ValueError):
            continue
        text = str(item.get("text", "")).strip()
        if text:
            segments.append({"start": start, "end": end, "text": text})
    result = payload.get("result", {})
    language = str(result.get("language", "ru")) if isinstance(result, dict) else "ru"
    return {
        "text": " ".join(item["text"] for item in segments),
        "language": language,
        "segments": segments,
    }


def _local_whisper_cpp_model() -> Path:
    from huggingface_hub import snapshot_download

    snapshot = Path(
        snapshot_download(
            repo_id=WINDOWS_VULKAN_WHISPER_REPO,
            allow_patterns=list(WINDOWS_VULKAN_WHISPER_PATTERNS),
            local_files_only=True,
        )
    )
    model = snapshot / WINDOWS_VULKAN_WHISPER_PATTERNS[0]
    if not model.is_file():
        raise RuntimeError("Локальну модель Whisper для Vulkan не знайдено")
    return model


def _run_whisper_cpp(command: list[str], environment: dict[str, str]) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
        **quiet_process_options(),
    )
    errors = []
    assert process.stderr is not None
    for line in process.stderr:
        clean = line.rstrip()
        if clean:
            errors.append(clean)
            print(clean, file=sys.stderr, flush=True)
    return process.wait(), "\n".join(errors[-80:])


def whisper_cpp_transcribe(
    input_path: Path, output_path: Path, preference: str, language_mode: str = "standard"
) -> dict:
    executable = runtime_tool_path("whisper-cli")
    if not executable:
        raise RuntimeError("Компонент Whisper Vulkan відсутній у цій збірці Rothbald")
    try:
        vulkan_index = int(preference.split(":", 1)[1])
    except (IndexError, ValueError) as exc:
        raise RuntimeError("Некоректно збережений вибір Vulkan GPU") from exc

    output_prefix = output_path.with_suffix(".whispercpp")
    generated_json = output_prefix.with_suffix(output_prefix.suffix + ".json")
    base_command = [
        str(executable),
        "--model",
        str(_local_whisper_cpp_model()),
        "--file",
        str(input_path),
        "--language",
        whisper_language(language_mode) or "auto",
        "--beam-size",
        "5",
        "--output-json",
        "--output-file",
        str(output_prefix),
        "--print-progress",
    ]
    environment = os.environ.copy()
    environment["GGML_VK_VISIBLE_DEVICES"] = str(vulkan_index)
    return_code, gpu_errors = _run_whisper_cpp(base_command, environment)
    if return_code:
        print(
            "Vulkan GPU недоступна для цього файлу; повторюю розпізнавання на CPU.",
            file=sys.stderr,
            flush=True,
        )
        environment.pop("GGML_VK_VISIBLE_DEVICES", None)
        return_code, cpu_errors = _run_whisper_cpp(base_command + ["--no-gpu"], environment)
        if return_code:
            detail = cpu_errors or gpu_errors or "невідома помилка whisper.cpp"
            raise RuntimeError(detail[-3000:])
    try:
        payload = json.loads(generated_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Whisper Vulkan не створив коректний результат") from exc
    return parse_whisper_cpp_result(payload)


def main() -> None:
    if len(sys.argv) not in {4, 5}:
        raise SystemExit("usage: transcribe_video.py INPUT OUTPUT MODEL [LANGUAGE_MODE]")
    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    model = sys.argv[3]
    language_mode = sys.argv[4] if len(sys.argv) == 5 else "standard"

    preference = os.environ.get("ROTHBALD_DEVICE", "auto").lower()
    if sys.platform == "win32" and preference.startswith("vulkan:"):
        result = whisper_cpp_transcribe(input_path, output_path, preference, language_mode)
    elif sys.platform == "win32":
        result = faster_transcribe(input_path, model, language_mode)
    else:
        result = mlx_transcribe(input_path, model, language_mode)
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
