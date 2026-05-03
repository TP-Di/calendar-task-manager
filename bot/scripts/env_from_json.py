#!/usr/bin/env python3
"""
Конвертирует credentials.json (и опционально token.json) в строки для .env.

Без аргументов — ищет файлы в типичных местах (cwd, рядом со скриптом,
project root, ~/Downloads, Рабочий стол).

Использование:
    python3 env_from_json.py
    python3 env_from_json.py path/to/credentials.json
    python3 env_from_json.py credentials.json token.json

Чтобы сразу записать в .env:
    python3 env_from_json.py >> ../.env       # из bot/scripts/
"""

import json
import sys
from pathlib import Path


def search_paths(name: str) -> list[Path]:
    """Где искать credentials.json / token.json."""
    here = Path(__file__).resolve().parent       # bot/scripts/
    project = here.parent.parent                  # repo root
    home = Path.home()
    return [
        Path.cwd() / name,
        here / name,
        here.parent / name,                       # bot/
        project / name,                           # repo root
        home / "Downloads" / name,
        home / "Desktop" / name,
    ]


def find_file(name: str) -> Path | None:
    for p in search_paths(name):
        if p.is_file():
            return p
    return None


def to_env_line(key: str, path: Path) -> str:
    data = json.load(path.open())
    return f"{key}={json.dumps(data, ensure_ascii=False, separators=(',', ':'))}"


def emit(key: str, path: Path | None, default_name: str) -> None:
    if path is None:
        print(f"# (skip) {default_name} не найден", file=sys.stderr)
        print(f"#         положи файл в одну из:", file=sys.stderr)
        for p in search_paths(default_name):
            print(f"#           - {p}", file=sys.stderr)
        print(f"#         или передай путь аргументом: python {Path(__file__).name} <path>", file=sys.stderr)
        return
    if not path.is_file():
        print(f"# (skip) {path} не найден", file=sys.stderr)
        return
    try:
        print(to_env_line(key, path))
        print(f"# ✓ {key} взят из {path}", file=sys.stderr)
    except Exception as e:
        print(f"# (error) {path}: {e}", file=sys.stderr)


def main() -> None:
    args = sys.argv[1:]
    creds = Path(args[0]) if len(args) > 0 else find_file("credentials.json")
    token = Path(args[1]) if len(args) > 1 else find_file("token.json")

    emit("GOOGLE_CREDENTIALS_JSON", creds, "credentials.json")
    emit("GOOGLE_TOKEN_JSON",       token, "token.json")


if __name__ == "__main__":
    main()
