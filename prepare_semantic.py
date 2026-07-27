#!/usr/bin/env python3
from __future__ import annotations

import server


def main() -> None:
    from huggingface_hub import snapshot_download

    try:
        snapshot_download(
            repo_id=server.EMBEDDING_MODEL,
            allow_patterns=server.EMBEDDING_MODEL_FILES,
            local_files_only=True,
        )
    except Exception:
        snapshot_download(repo_id=server.EMBEDDING_MODEL, allow_patterns=server.EMBEDDING_MODEL_FILES)
    model = server.embedder()
    query = model.encode(["защита бизнеса от чрезмерных проверок"], "query")[0]
    passages = model.encode(
        [
            "Влада скоротить кількість перевірок і гарантує підприємцям стабільні правила роботи.",
            "Сьогодні очікується холодна погода та сильний вітер.",
        ],
        "passage",
    )
    assert float(passages[0] @ query) > float(passages[1] @ query)
    print("Смисловий пошук готовий")


if __name__ == "__main__":
    main()
