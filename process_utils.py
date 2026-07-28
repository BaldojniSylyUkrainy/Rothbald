from __future__ import annotations

import subprocess
import sys


def quiet_process_options(*, new_process_group: bool = False) -> dict[str, int]:
    """Hide console windows for child processes launched by the windowed Windows app."""
    if sys.platform != "win32":
        return {}
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    if new_process_group:
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    return {"creationflags": flags}
