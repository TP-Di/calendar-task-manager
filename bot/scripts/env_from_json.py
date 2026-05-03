#!/usr/bin/env python3
"""
Авторизуется в Google Calendar/Tasks → печатает готовые строки для .env.

Запуск:
    pip install google-auth-oauthlib
    python env_from_json.py

Что делает:
1. Если credentials.json есть рядом — берёт его.
   Иначе — попросит вставить содержимое прямо в терминал.
2. Открывает браузер с Google consent.
3. Ловит редирект на localhost, обменивает code на token.
4. Печатает в stdout две строки для копипаста в DigitalOcean
   (Settings → Environment Variables, encrypted):
       GOOGLE_CREDENTIALS_JSON=...
       GOOGLE_TOKEN_JSON=...
"""

import json
import sys
from pathlib import Path

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]


def search_paths() -> list[Path]:
    here = Path(__file__).resolve().parent
    home = Path.home()
    return [
        Path.cwd() / "credentials.json",
        here / "credentials.json",
        here.parent / "credentials.json",
        here.parent.parent / "credentials.json",
        home / "Downloads" / "credentials.json",
        home / "Desktop" / "credentials.json",
    ]


def find_creds_file() -> Path | None:
    for p in search_paths():
        if p.is_file():
            return p
    return None


def prompt_paste_creds() -> Path:
    """Просит пользователя вставить содержимое credentials.json в терминал."""
    print("=" * 70, file=sys.stderr)
    print("credentials.json не найден.", file=sys.stderr)
    print("", file=sys.stderr)
    print("Вставь СЮДА содержимое credentials.json одним блоком,", file=sys.stderr)
    print("потом нажми Enter и:", file=sys.stderr)
    print("  Windows:  Ctrl+Z, затем Enter", file=sys.stderr)
    print("  Mac/Linux: Ctrl+D", file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    raw = sys.stdin.read().strip()
    if not raw:
        print("Пусто. Прерываю.", file=sys.stderr)
        sys.exit(1)

    # Валидация: должно быть валидным JSON
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"Не валидный JSON: {e}", file=sys.stderr)
        sys.exit(1)

    inner = data.get("installed") or data.get("web")
    if not inner or not inner.get("client_id"):
        print("В JSON нет 'installed.client_id' или 'web.client_id'. "
              "Это точно credentials.json от OAuth client?", file=sys.stderr)
        sys.exit(1)

    # Сохраняем рядом со скриптом
    target = Path(__file__).resolve().parent / "credentials.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    print(f"# ✓ Сохранил credentials.json в {target}", file=sys.stderr)
    return target


def main() -> None:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Нет библиотеки google-auth-oauthlib. Установи:", file=sys.stderr)
        print("  pip install google-auth-oauthlib", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]
    if args:
        creds_path = Path(args[0])
        if not creds_path.is_file():
            print(f"Файл не найден: {creds_path}", file=sys.stderr)
            sys.exit(1)
    else:
        creds_path = find_creds_file()
        if creds_path is None:
            creds_path = prompt_paste_creds()

    print(f"# Использую: {creds_path}", file=sys.stderr)
    print(f"# Запускаю OAuth flow — сейчас откроется браузер...", file=sys.stderr)

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    # run_local_server поднимает временный HTTP-сервер, открывает браузер
    # с Google consent, ловит редирект и обменивает code → token.
    creds = flow.run_local_server(port=0, open_browser=True)

    # Сохраняем token.json рядом
    token_path = creds_path.parent / "token.json"
    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"# ✓ Токен сохранён: {token_path}", file=sys.stderr)

    # Готовые строки для .env / DO Environment Variables
    creds_data = json.load(creds_path.open())
    token_data = json.loads(creds.to_json())
    print(f"GOOGLE_CREDENTIALS_JSON={json.dumps(creds_data, ensure_ascii=False, separators=(',', ':'))}")
    print(f"GOOGLE_TOKEN_JSON={json.dumps(token_data, ensure_ascii=False, separators=(',', ':'))}")
    print("", file=sys.stderr)
    print("# ✓ Готово. Скопируй обе строки → DO → Settings →", file=sys.stderr)
    print("#   App-Level Environment Variables (как Encrypted) → Save.", file=sys.stderr)


if __name__ == "__main__":
    main()
