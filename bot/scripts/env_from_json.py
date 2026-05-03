#!/usr/bin/env python3
"""
Авторизуется в Google Calendar/Tasks через локальный браузер и печатает
готовые строки для .env (GOOGLE_CREDENTIALS_JSON + GOOGLE_TOKEN_JSON).

Использование:
    pip install google-auth-oauthlib
    python env_from_json.py

Без аргументов скрипт ищет credentials.json в типичных местах
(cwd, рядом со скриптом, project root, ~/Downloads, ~/Desktop).
Можно передать путь явно:
    python env_from_json.py path/to/credentials.json

Что делает:
1. Находит credentials.json
2. Запускает локальный OAuth flow: открывает браузер, ждёт consent,
   ловит редирект на localhost, обменивает code на token.
3. Печатает в stdout две строки готовые для .env:
       GOOGLE_CREDENTIALS_JSON=<json одной строкой>
       GOOGLE_TOKEN_JSON=<json одной строкой>
4. Также сохраняет token.json рядом для удобства.

Ставишь обе строки в DigitalOcean App Platform → Settings →
Environment Variables (как Secret) — и бот не будет просить /reauth
при каждом редеплое.
"""

import json
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]


def search_paths(name: str) -> list[Path]:
    here = Path(__file__).resolve().parent
    project = here.parent.parent
    home = Path.home()
    return [
        Path.cwd() / name,
        here / name,
        here.parent / name,
        project / name,
        home / "Downloads" / name,
        home / "Desktop" / name,
    ]


def find_creds() -> Path | None:
    for p in search_paths("credentials.json"):
        if p.is_file():
            return p
    return None


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Нет библиотеки google-auth-oauthlib. Установи:", file=sys.stderr)
        print("  pip install google-auth-oauthlib", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]
    creds_path = Path(args[0]) if args else find_creds()

    if creds_path is None or not creds_path.is_file():
        print("credentials.json не найден. Положи его в одну из:", file=sys.stderr)
        for p in search_paths("credentials.json"):
            print(f"  - {p}", file=sys.stderr)
        print("Или укажи путь: python env_from_json.py <path>", file=sys.stderr)
        sys.exit(1)

    print(f"# Использую credentials: {creds_path}", file=sys.stderr)
    print(f"# Сейчас откроется браузер для авторизации Google...", file=sys.stderr)

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    # run_local_server поднимает временный HTTP-сервер на локалхосте,
    # открывает браузер с Google consent, ловит редирект и обменивает code.
    creds = flow.run_local_server(port=0, open_browser=True)

    # Сохраняем token.json рядом со скриптом для удобства
    token_path = creds_path.parent / "token.json"
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"# ✓ Токен сохранён: {token_path}", file=sys.stderr)

    # Готовые строки для .env / DO Environment Variables
    creds_data = json.load(creds_path.open())
    token_data = json.loads(creds.to_json())
    print(f"GOOGLE_CREDENTIALS_JSON={json.dumps(creds_data, ensure_ascii=False, separators=(',', ':'))}")
    print(f"GOOGLE_TOKEN_JSON={json.dumps(token_data, ensure_ascii=False, separators=(',', ':'))}")
    print("# ✓ Готово. Скопируй обе строки в DO → Settings → Environment Variables (как Secret).", file=sys.stderr)


if __name__ == "__main__":
    main()
