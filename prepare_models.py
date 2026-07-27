#!/usr/bin/env python3
from __future__ import annotations

from model_manager import get_model_manager
import server


def main() -> None:
    manager = get_model_manager(server.DATA_DIR)
    manager.start(force=True)
    state = manager.wait()
    print(state["phase"])


if __name__ == "__main__":
    main()
