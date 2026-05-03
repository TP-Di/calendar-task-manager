#!/usr/bin/env python3
"""
Конвертирует credentials.json (и опционально token.json) в строки для .env.

Использование:
    python3 scripts/env_from_json.py
    python3 scripts/env_from_json.py path/to/credentials.json
    python3 scripts/env_from_json.py credentials.json token.json

Печатает в stdout строки, которые можно добавить в .env:
    GOOGLE_CREDENTIALS_JSON=<json одной строкой>
    GOOGLE_TOKEN_JSON=<json одной строкой>

Чтобы сразу записать:
    python3 scripts/env_from_json.py >> .env
"""

import json
import sys
from pathlib import Path


def to_env_line(key: str, path: Path) -> str | None:
    if not path.is_file():
        print(f"# (skip) {path} не найден", file=sys.stderr)
        return None
    try:
        data = json.load(path.open())
    except Exception as e:
        print(f"# (error) {path}: {e}", file=sys.stderr)
        return None
    return f"{key}={json.dumps(data, ensure_ascii=False, separators=(',', ':'))}"


def main() -> None:
    args = sys.argv[1:]
    creds_path = Path(args[0] if len(args) > 0 else "credentials.json")
    token_path = Path(args[1] if len(args) > 1 else "token.json")

    for key, path in [
        ("GOOGLE_CREDENTIALS_JSON", creds_path),
        ("GOOGLE_TOKEN_JSON",       token_path),
    ]:
        line = to_env_line(key, path)
        if line:
            print(line)


if __name__ == "__main__":
    main()
