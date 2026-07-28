from __future__ import annotations

import fnmatch
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


IS_WINDOWS = sys.platform == "win32"
WHISPER_REPO = (
    "mobiuslabsgmbh/faster-whisper-large-v3-turbo"
    if IS_WINDOWS
    else "mlx-community/whisper-large-v3-turbo"
)
WHISPER_PATTERNS = (
    ("config.json", "model.bin", "tokenizer.json", "vocabulary.*")
    if IS_WINDOWS
    else ("config.json", "weights.safetensors")
)
EMBEDDING_REPO = "intfloat/multilingual-e5-large-instruct"
EMBEDDING_PATTERNS = (
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
)


@dataclass(frozen=True)
class ModelSpec:
    key: str
    label: str
    repo_id: str
    patterns: tuple[str, ...]


MODEL_SPECS = (
    ModelSpec("speech", "Розпізнавання мовлення", WHISPER_REPO, WHISPER_PATTERNS),
    ModelSpec("meaning", "Пошук за змістом", EMBEDDING_REPO, EMBEDDING_PATTERNS),
)


class ModelManager:
    """Checks and downloads model files while exposing UI-safe progress snapshots."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.manifest_path = state_dir / "model-manifest.json"
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._state = {
            "status": "idle",
            "phase": "Очікую",
            "percent": 0,
            "error": None,
            "offline": False,
            "eta_seconds": None,
            "bytes_per_second": 0,
            "models": [
                {
                    "key": spec.key,
                    "label": spec.label,
                    "repo": spec.repo_id,
                    "status": "waiting",
                    "percent": 0,
                    "downloaded": 0,
                    "total": 0,
                    "detail": "Очікує перевірки",
                    "eta_seconds": None,
                    "bytes_per_second": 0,
                }
                for spec in MODEL_SPECS
            ],
        }

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def start(self, force: bool = False) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if self._state["status"] == "ready" and not force:
                return
            self._state.update(status="checking", phase="Перевіряю актуальність", percent=1, error=None)
            self._thread = threading.Thread(target=self._run, daemon=True, name="rothbald-models")
            self._thread.start()

    def wait(self) -> dict:
        self.start()
        thread = self._thread
        if thread:
            thread.join()
        state = self.snapshot()
        if state["status"] != "ready":
            raise RuntimeError(state.get("error") or "Моделі не готові")
        return state

    def _set(self, **changes) -> None:
        with self._lock:
            self._state.update(changes)

    def _set_model(self, key: str, **changes) -> None:
        with self._lock:
            model = next(item for item in self._state["models"] if item["key"] == key)
            model.update(changes)
            totals = [max(1, int(item["total"])) for item in self._state["models"]]
            completed = [
                min(total, int(item["downloaded"])) if item["status"] != "ready" else total
                for item, total in zip(self._state["models"], totals)
            ]
            self._state["percent"] = min(100, round(sum(completed) / sum(totals) * 100))
            active = next((item for item in self._state["models"] if item["status"] == "downloading"), None)
            self._state["eta_seconds"] = active.get("eta_seconds") if active else None
            self._state["bytes_per_second"] = active.get("bytes_per_second", 0) if active else 0

    @staticmethod
    def _matches(filename: str, patterns: tuple[str, ...]) -> bool:
        return any(fnmatch.fnmatch(filename, pattern) for pattern in patterns)

    @staticmethod
    def _locally_ready(spec: ModelSpec, revision: str | None = None) -> bool:
        try:
            from huggingface_hub import snapshot_download

            snapshot = Path(snapshot_download(
                repo_id=spec.repo_id,
                revision=revision,
                allow_patterns=list(spec.patterns),
                local_files_only=True,
            ))
            filenames = [
                path.relative_to(snapshot).as_posix()
                for path in snapshot.rglob("*")
                if path.is_file()
            ]
            return all(
                any(fnmatch.fnmatch(filename, pattern) for filename in filenames)
                for pattern in spec.patterns
            )
        except Exception:
            return False

    def _load_manifest(self) -> dict:
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_manifest(self, payload: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.manifest_path)

    def _remote_files(self, spec: ModelSpec) -> tuple[str, list[tuple[str, int]]]:
        from huggingface_hub import HfApi

        info = HfApi().model_info(spec.repo_id, files_metadata=True)
        files = []
        for sibling in info.siblings or []:
            if not self._matches(sibling.rfilename, spec.patterns):
                continue
            size = int(getattr(sibling, "size", 0) or 0)
            files.append((sibling.rfilename, size))
        if not files:
            raise RuntimeError(f"У репозиторії {spec.repo_id} не знайдено потрібних файлів")
        return str(info.sha or "main"), files

    def _download_file(
        self,
        spec: ModelSpec,
        filename: str,
        revision: str,
        expected_size: int,
        base_downloaded: int,
        total: int,
        download_started_at: float,
    ) -> None:
        from huggingface_hub import hf_hub_download
        from tqdm.auto import tqdm

        manager = self

        class ModelProgress(tqdm):
            def update(self, amount=1):
                result = super().update(amount)
                current = min(expected_size, int(getattr(self, "n", 0)))
                downloaded = min(total, base_downloaded + current)
                elapsed = max(0.1, time.monotonic() - download_started_at)
                speed = downloaded / elapsed
                remaining = max(0, total - downloaded)
                manager._set_model(
                    spec.key,
                    downloaded=downloaded,
                    percent=round(downloaded / max(1, total) * 100),
                    bytes_per_second=round(speed),
                    eta_seconds=round(remaining / speed) if speed > 0 and downloaded > 0 else None,
                )
                return result

        hf_hub_download(
            repo_id=spec.repo_id,
            filename=filename,
            revision=revision,
            tqdm_class=ModelProgress,
        )

    def _run(self) -> None:
        manifest = self._load_manifest()
        next_manifest = {"platform": sys.platform, "models": {}}
        try:
            remote: dict[str, tuple[str, list[tuple[str, int]]]] = {}
            remote_available = True
            for spec in MODEL_SPECS:
                manifest_revision = manifest.get("models", {}).get(spec.key, {}).get("revision")
                local_ready = self._locally_ready(spec, manifest_revision)
                self._set_model(
                    spec.key,
                    status="checking",
                    detail="Перевіряю локальні файли й актуальність",
                    percent=4 if local_ready else 0,
                    downloaded=4 if local_ready else 0,
                    total=100,
                )
                try:
                    remote[spec.key] = self._remote_files(spec)
                except Exception:
                    remote_available = False
                    if not local_ready:
                        raise RuntimeError(
                            "Немає з’єднання з Hugging Face, а потрібні моделі ще не встановлені."
                        )
                    remote[spec.key] = (
                        str(manifest.get("models", {}).get(spec.key, {}).get("revision", "offline")),
                        [],
                    )

            self._set(offline=not remote_available)
            for spec in MODEL_SPECS:
                revision, files = remote[spec.key]
                old_revision = manifest.get("models", {}).get(spec.key, {}).get("revision")
                local_ready = self._locally_ready(
                    spec,
                    revision if remote_available else old_revision,
                )
                needs_download = not local_ready
                if not needs_download:
                    self._set_model(
                        spec.key, status="ready", percent=100, downloaded=100, total=100,
                        detail="Актуальна версія вже на комп’ютері", eta_seconds=0, bytes_per_second=0,
                    )
                elif not files:
                    self._set_model(
                        spec.key, status="ready", percent=100, downloaded=100, total=100,
                        detail="Локальна версія готова · перевірка онлайн недоступна", eta_seconds=0, bytes_per_second=0,
                    )
                else:
                    total = max(1, sum(size for _, size in files))
                    self._set(
                        status="downloading",
                        phase=f"Завантажую: {spec.label.lower()}",
                    )
                    self._set_model(
                        spec.key, status="downloading", total=total, downloaded=0, percent=0,
                        detail="Завантаження файлів моделі", eta_seconds=None, bytes_per_second=0,
                    )
                    completed = 0
                    download_started_at = time.monotonic()
                    for filename, size in files:
                        self._set_model(spec.key, detail=f"Файл: {Path(filename).name}")
                        self._download_file(
                            spec, filename, revision, size, completed, total, download_started_at
                        )
                        completed += size
                        self._set_model(
                            spec.key,
                            downloaded=min(total, completed),
                            percent=round(min(total, completed) / total * 100),
                        )
                    if not self._locally_ready(spec, revision):
                        raise RuntimeError(f"Не вдалося перевірити завантажену модель {spec.label}")
                    self._set_model(
                        spec.key, status="ready", percent=100, downloaded=total,
                        detail="Готова до роботи", eta_seconds=0, bytes_per_second=0,
                    )
                next_manifest["models"][spec.key] = {"repo": spec.repo_id, "revision": revision}

            if remote_available:
                self._save_manifest(next_manifest)
            self._set(
                status="ready", phase="Усе готово", percent=100, error=None,
                eta_seconds=0, bytes_per_second=0,
            )
        except Exception as exc:
            self._set(status="error", phase="Потрібна увага", error=str(exc))


_manager: ModelManager | None = None


def get_model_manager(state_dir: Path) -> ModelManager:
    global _manager
    if _manager is None:
        _manager = ModelManager(state_dir)
    return _manager


def reset_model_manager() -> None:
    """Test helper."""
    global _manager
    _manager = None
