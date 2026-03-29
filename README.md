# Telegram AI Scheduler Bot

![header](https://drive.google.com/uc?id=1bZpwudnXLsSWLIdvTQk22iPTbzBHAgEq)

Персональный бот-планировщик с AI-агентом. Управляет Google Calendar и Google Tasks через чат на естественном языке.

**Стек:** Python 3.11 · aiogram 3 · Groq (llama-4-maverick) · Google Calendar/Tasks API · aiosqlite · APScheduler · Docker

---

## Что умеет

- **Общение на русском** — просто пишешь что нужно, агент разбирается сам
- **Google Calendar** — читает, создаёт, перемещает, удаляет события
- **Google Tasks** — список задач, создание, выполнение, перенос дедлайнов
- **Подтверждения** — любое изменение сначала показывается на экране, потом ждёт кнопку «Да»
- **Приоритеты** — при конфликтах расписания двигает задачи снизу вверх: курсы → проекты → магистратура → работа → бакалавр
- **Теги** — `[HARD]` (не трогать никогда), `[SOFT]` (можно двигать), `[PRIORITY:x]`, `[DEPENDS:название]`
- **Утренний брифинг** — в настраиваемое время отправляет сводку на день
- **Напоминания** — за 24ч / 3ч / 1ч до дедлайна, с кнопкой снуза
- **Тихие часы** — ночью напоминания не отправляет
- **Тепловая карта** — `/heatmap` показывает расписание недели + pie chart нагрузки по категориям
- **Парсинг PDF** — загружаешь файл с расписанием, агент предлагает создать события и задачи
- **Whitelist** — отвечает только указанным пользователям, остальных молча игнорирует

---

## Подготовка: получить токены

### 1. Telegram Bot Token

1. Открыть [@BotFather](https://t.me/BotFather) в Telegram
2. Отправить `/newbot`, задать имя и username
3. Скопировать токен вида `7123456789:AAF...`

Свой Telegram ID можно узнать у [@userinfobot](https://t.me/userinfobot).

### 2. Groq API Key

1. Зарегистрироваться на [console.groq.com](https://console.groq.com)
2. Перейти в **API Keys → Create API Key**
3. Скопировать ключ вида `gsk_...`

### 3. Google Credentials

Нужно создать проект в Google Cloud и включить два API.

#### 3.1 Создать проект и включить API

1. Открыть [console.cloud.google.com](https://console.cloud.google.com)
2. Создать новый проект (или выбрать существующий)
3. Перейти в **APIs & Services → Enable APIs and Services**
4. Включить **Google Calendar API**
5. Включить **Tasks API**

#### 3.2 Настроить OAuth экран

1. Перейти в **APIs & Services → OAuth consent screen**
2. Выбрать **External**, нажать **Create**
3. Заполнить обязательные поля (App name, User support email, Developer contact)
4. На шаге **Scopes** — можно пропустить, нажать **Save and Continue**
5. На шаге **Test users** добавить свой Google аккаунт
6. Нажать **Save and Continue**

#### 3.3 Создать OAuth Client ID

1. Перейти в **APIs & Services → Credentials**
2. Нажать **Create Credentials → OAuth client ID**
3. Application type: **Desktop app**
4. Нажать **Create**
5. Нажать **Download JSON** — скачается файл `credentials.json`

#### 3.4 Получить содержимое credentials.json

Открыть скачанный файл и скопировать его содержимое — это и есть значение для `GOOGLE_CREDENTIALS_JSON`:

```bash
cat ~/Downloads/client_secret_....json
```

Выглядит примерно так:
```json
{"installed":{"client_id":"123-abc.apps.googleusercontent.com","project_id":"my-project","auth_uri":"https://accounts.google.com/o/oauth2/auth","token_uri":"https://oauth2.googleapis.com/token","client_secret":"GOCSPX-...","redirect_uris":["http://localhost"]}}
```

---

## Установка и первый запуск

### Локально (разработка)

```bash
git clone <repo>
cd calendar-task-manager/bot

# Зависимости
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Конфиг
cp .env.example .env
```

Открыть `.env` и заполнить:

```env
BOT_TOKEN=7123456789:AAF...
ALLOWED_IDS=123456789
GROQ_API_KEY=gsk_...
GOOGLE_CREDENTIALS_JSON={"installed":{...}}   # содержимое credentials.json
TIMEZONE=Europe/Moscow                         # твоя временная зона
```

#### Первая авторизация Google

При первом запуске бот откроет браузер для OAuth авторизации:

```bash
python main.py
```

В консоли появится ссылка — открыть её в браузере, войти в Google аккаунт, разрешить доступ. После этого в папке `data/` появится `token.json`.

Бот запустится и будет работать. При следующих запусках авторизация не нужна — токен сохранён.

---

## Деплой на сервер (Docker)

### Шаг 1. Получить token.json

Сначала нужно авторизоваться локально (см. выше), чтобы получить `data/token.json`. Это делается один раз.

### Шаг 2. Скопировать token.json в .env

```bash
# На локальной машине после авторизации:
cat bot/data/token.json
```

Скопировать вывод и вставить в `.env` на сервере:

```env
GOOGLE_TOKEN_JSON={"token":"ya29....","refresh_token":"1//...","token_uri":"...","client_id":"...","client_secret":"...","scopes":[...],"expiry":"..."}
```

Это позволяет не монтировать файлы — все секреты в переменных окружения.

### Шаг 3. Залить на сервер и запустить

```bash
# На сервере (DO Droplet или любой Linux с Docker)
git clone <repo>
cd calendar-task-manager

# Создать .env рядом с docker-compose.yml
cp bot/.env.example bot/.env
nano bot/.env   # заполнить все переменные

# Запустить
docker-compose up -d

# Проверить логи
docker-compose logs -f
```

### Шаг 4. Проверить что работает

Отправить боту `/start` — должен ответить приветствием.

---

## Переменные окружения

| Переменная | Описание | Пример |
|---|---|---|
| `BOT_TOKEN` | Токен из BotFather | `7123:AAF...` |
| `ALLOWED_IDS` | Telegram ID через запятую | `123456789,987654321` |
| `GROQ_API_KEY` | Ключ Groq API | `gsk_...` |
| `GROQ_MODEL` | Модель Groq | `meta-llama/llama-4-maverick-17b-128e-instruct` |
| `GOOGLE_CREDENTIALS_JSON` | Содержимое credentials.json | `{"installed":{...}}` |
| `GOOGLE_TOKEN_JSON` | Содержимое token.json (опционально) | `{"token":"ya29..."}` |
| `GOOGLE_TOKEN_PATH` | Путь для сохранения токена | `data/token.json` |
| `TIMEZONE` | Временная зона (IANA) | `Europe/Moscow` |
| `BRIEFING_TIME` | Время брифинга в локальной зоне (ЧЧ:ММ) | `06:00` |
| `REMINDER_INTERVAL_HOURS` | Интервал проверки дедлайнов (ч) | `1` |
| `QUIET_HOUR_START` | Начало тихих часов | `23` |
| `QUIET_HOUR_END` | Конец тихих часов | `6` |
| `LOG_LEVEL` | Уровень логов | `INFO` |
| `DB_PATH` | Путь к SQLite базе | `data/bot.db` |

---

## Команды бота

| Команда | Описание |
|---|---|
| `/start` | Приветствие |
| `/help` | Список команд |
| `/status` | Активные задачи и ближайшие события |
| `/load` | Нагрузка по дням на текущей неделе |
| `/heatmap` | Тепловая карта расписания на неделю |
| `/done Название` | Отметить задачу выполненной |
| `/postpone Название время` | Отложить задачу |
| `/upload` | Загрузить PDF с расписанием |
| `/clear` | Очистить историю диалога |

Или просто пиши сообщения — агент поймёт:

> «Создай событие "Защита диплома" в пятницу в 10 утра на 2 часа»
> «Покажи что у меня на этой неделе»
> «Перенеси встречу с командой на завтра»
> «Добавь задачу сдать отчёт до 20 января»

---

## Теги для событий

Добавляй в описание события в Google Calendar:

- `[HARD]` — событие нельзя двигать никогда (экзамен, защита)
- `[SOFT]` — можно сдвинуть при конфликте
- `[PRIORITY:бакалавр]` — приоритет из списка
- `[DEPENDS:название]` — зависит от другого события

Приоритеты (от высшего к низшему): `бакалавр` → `работа` → `магистратура` → `проекты` → `курсы`

---

## Структура проекта

```
bot/
├── main.py                    # Точка входа, APScheduler, polling
├── requirements.txt
├── Dockerfile
├── .env.example
└── app/
    ├── config.py              # Конфиг из .env
    ├── middleware/
    │   └── whitelist.py       # Фильтрация по ALLOWED_IDS
    ├── handlers/
    │   ├── commands.py        # /start /help /status /load /heatmap /done /postpone /clear
    │   ├── messages.py        # Текстовые сообщения → агент + inline подтверждения
    │   └── documents.py       # /upload — PDF и фото
    ├── services/
    │   ├── agent.py           # Groq tool calling loop
    │   ├── calendar.py        # Google Calendar API
    │   ├── tasks.py           # Google Tasks API
    │   ├── briefing.py        # Утренний брифинг + воскресный ретро
    │   └── reminders.py       # Напоминания с эскалацией и snooze
    ├── db/
    │   └── database.py        # История диалога, контекст, бэкап
    └── tools/
        └── definitions.py     # Описания tools для Groq
```

---

## Решение проблем

**Бот не отвечает**
- Проверить `ALLOWED_IDS` — там должен быть твой Telegram ID
- Проверить логи: `docker-compose logs -f`

**Ошибка Google API**
- Убедиться что включены Google Calendar API и Tasks API в Cloud Console
- Проверить что в Test Users добавлен твой аккаунт (пока приложение не верифицировано)
- Попробовать переавторизоваться: удалить `data/token.json` и перезапустить локально

**Ошибка Groq API**
- Проверить `GROQ_API_KEY`
- Убедиться что модель `meta-llama/llama-4-maverick-17b-128e-instruct` доступна в аккаунте

**token.json устарел**
- Токен обновляется автоматически если есть `refresh_token`
- Если всё равно ошибка — переавторизоваться локально и обновить `GOOGLE_TOKEN_JSON` в `.env` на сервере
