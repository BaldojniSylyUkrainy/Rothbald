from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path


GIB = 1024 ** 3
MINIMUM_RAM = 8 * GIB
RECOMMENDED_RAM = 16 * GIB
MINIMUM_DISK = 6 * GIB
RECOMMENDED_DISK = 8 * GIB
MINIMUM_CPU_CORES = 4


def _physical_memory() -> int:
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return int(status.ullTotalPhys)
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "hw.memsize"],
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
            return int(result.stdout.strip())
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 0


def _storage_root(path: Path) -> Path:
    candidate = path.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for item in value.split("."):
        try:
            parts.append(int(item))
        except ValueError:
            break
    return tuple(parts)


def _nvidia_gpus() -> list[dict]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [executable, "--query-gpu=index,name,memory.total", "--format=csv,noheader,nounits"],
            check=True,
            capture_output=True,
            text=True,
            timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        try:
            memory = int(parts[2]) * 1024 ** 2
        except ValueError:
            memory = 0
        gpus.append({"index": int(parts[0]), "name": parts[1], "memory": memory})
    return gpus


def _cuda_device_count() -> int:
    if sys.platform != "win32":
        return 0
    try:
        import ctranslate2

        return max(0, int(ctranslate2.get_cuda_device_count()))
    except Exception:
        return 0


class HardwarePreflight:
    """Detects whether the local machine can safely download and run the models."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.settings_path = state_dir / "hardware.json"

    def _load(self) -> dict:
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, payload: dict) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.settings_path)

    def inspect(self) -> dict:
        machine = platform.machine().lower()
        ram = _physical_memory()
        cpu_cores = max(1, os.cpu_count() or 1)
        try:
            disk_free = int(shutil.disk_usage(_storage_root(self.state_dir)).free)
        except OSError:
            disk_free = 0

        supported = (sys.platform == "darwin" and machine == "arm64") or (
            sys.platform == "win32" and machine in {"amd64", "x86_64"}
        )
        os_version = platform.mac_ver()[0] if sys.platform == "darwin" else platform.version()
        devices = []
        if sys.platform == "darwin" and machine == "arm64":
            devices = [
                {"key": "auto", "label": "Автоматично — Apple GPU (MLX)", "available": True, "recommended": True},
                {"key": "apple", "label": "Apple GPU (MLX)", "available": True, "recommended": False},
            ]
        else:
            cuda_count = _cuda_device_count()
            gpus = _nvidia_gpus()
            devices = [
                {
                    "key": "auto",
                    "label": "Автоматично — найкращий доступний пристрій",
                    "available": True,
                    "recommended": True,
                },
                {"key": "cpu", "label": "Процесор (CPU) — повільніше", "available": True, "recommended": False},
            ]
            for gpu in gpus:
                available = gpu["index"] < cuda_count
                memory = f" · {gpu['memory'] / GIB:.0f} ГБ" if gpu["memory"] else ""
                devices.append(
                    {
                        "key": f"cuda:{gpu['index']}",
                        "label": f"{gpu['name']}{memory}" + ("" if available else " — CUDA недоступна"),
                        "available": available,
                        "recommended": available and gpu["index"] == 0,
                    }
                )
            known_indices = {gpu["index"] for gpu in gpus}
            for index in range(cuda_count):
                if index not in known_indices:
                    devices.append(
                        {
                            "key": f"cuda:{index}",
                            "label": f"NVIDIA GPU {index + 1} (CUDA)",
                            "available": True,
                            "recommended": index == 0,
                        }
                    )

        warnings = []
        blockers = []
        if not supported:
            blockers.append("Ця збірка підтримує Apple Silicon або 64-бітну Windows x64.")
        if sys.platform == "darwin" and os_version and _version_tuple(os_version) < (14, 0):
            blockers.append("Потрібна macOS 14.0 або новіша для MLX, PyTorch і QtWebEngine.")
        if sys.platform == "win32":
            try:
                windows_build = int(sys.getwindowsversion().build)
            except (AttributeError, ValueError):
                windows_build = 0
            if windows_build and windows_build < 19045:
                blockers.append("Потрібна Windows 10 22H2 або Windows 11.")
        if ram and ram < MINIMUM_RAM:
            blockers.append("Менше 8 ГБ оперативної пам’яті: великі моделі можуть не запуститися.")
        elif ram and ram < RECOMMENDED_RAM:
            warnings.append("Оперативної пам’яті менше рекомендованих 16 ГБ — великі відео оброблятимуться повільніше.")
        if disk_free and disk_free < MINIMUM_DISK:
            blockers.append("Для застосунку, моделей і робочого кешу потрібно щонайменше 6 ГБ вільного місця.")
        elif disk_free and disk_free < RECOMMENDED_DISK:
            warnings.append("Вільного місця менше рекомендованих 8 ГБ — для наступного оновлення моделей може забракнути запасу.")
        if cpu_cores < MINIMUM_CPU_CORES:
            warnings.append("Менше 4 логічних ядер — інтерфейс і розпізнавання можуть суттєво гальмувати.")
        if sys.platform == "win32" and not any(item["key"].startswith("cuda:") and item["available"] for item in devices):
            warnings.append("Сумісну NVIDIA CUDA не знайдено. Rothbald працюватиме на CPU, але розпізнавання буде повільнішим.")

        identity = {
            "platform": sys.platform,
            "machine": machine,
            "ram_tier": 0 if not ram else 1 if ram < MINIMUM_RAM else 2 if ram < RECOMMENDED_RAM else 3,
            "cpu_cores": cpu_cores,
            "os_version": os_version,
            "disk_tier": 0 if not disk_free else 1 if disk_free < MINIMUM_DISK else 2 if disk_free < RECOMMENDED_DISK else 3,
            "devices": [(item["key"], item["available"]) for item in devices],
        }
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        saved = self._load()
        selected = str(saved.get("device", "auto"))
        valid_devices = {item["key"] for item in devices if item["available"]}
        if selected not in valid_devices:
            selected = "auto"
        accepted = not blockers and saved.get("fingerprint") == fingerprint and bool(saved.get("accepted_at"))
        performance = "blocked" if blockers else "limited" if warnings else "recommended"
        return {
            "platform": sys.platform,
            "platform_label": (
                f"macOS {os_version} · Apple Silicon" if sys.platform == "darwin"
                else f"Windows {os_version} · x64" if sys.platform == "win32"
                else f"{platform.system()} {os_version}".strip()
            ),
            "machine": machine,
            "ram_bytes": ram,
            "disk_free_bytes": disk_free,
            "cpu_cores": cpu_cores,
            "devices": devices,
            "selected_device": selected,
            "warnings": warnings,
            "blockers": blockers,
            "performance": performance,
            "fingerprint": fingerprint,
            "accepted": accepted,
            "requires_confirmation": not accepted,
        }

    def confirm(self, device: str) -> dict:
        report = self.inspect()
        if report["blockers"]:
            raise ValueError(" ".join(report["blockers"]))
        valid_devices = {item["key"] for item in report["devices"] if item["available"]}
        if device not in valid_devices:
            raise ValueError("Обраний обчислювальний пристрій недоступний")
        self._save(
            {
                "fingerprint": report["fingerprint"],
                "device": device,
                "accepted_at": time.time(),
            }
        )
        os.environ["ROTHBALD_DEVICE"] = device
        return self.inspect()

    def apply_saved_device(self) -> bool:
        report = self.inspect()
        if not report["accepted"]:
            return False
        os.environ["ROTHBALD_DEVICE"] = report["selected_device"]
        return True
