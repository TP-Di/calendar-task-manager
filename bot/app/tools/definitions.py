"""
Описания tools для Groq tool calling.
Каждый tool соответствует методу в services/calendar.py или services/tasks.py.
"""

TOOLS: list[dict] = [
    # -----------------------------------------------------------------------
    # Google Calendar
    # -----------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "get_events",
            "description": (
                "Получить события из Google Calendar за указанный период. "
                "Используй для просмотра расписания, поиска свободных слотов, "
                "проверки конфликтов."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {
                        "type": "string",
                        "description": "Начало периода в формате ISO 8601, например '2024-01-15T00:00:00'",
                    },
                    "date_to": {
                        "type": "string",
                        "description": "Конец периода в формате ISO 8601, например '2024-01-15T23:59:59'",
                    },
                },
                "required": ["date_from", "date_to"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_event",
            "description": (
                "Создать новое событие в Google Calendar. "
                "ВСЕГДА требует подтверждения пользователя перед выполнением."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Название события",
                    },
                    "start": {
                        "type": "string",
                        "description": "Начало события ISO 8601, например '2024-01-15T10:00:00'",
                    },
                    "end": {
                        "type": "string",
                        "description": "Конец события ISO 8601, например '2024-01-15T11:00:00'",
                    },
                    "description": {
                        "type": "string",
                        "description": "Описание события (опционально). Можно добавить теги [HARD], [SOFT], [PRIORITY:x], [DEPENDS:название]",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Тег: HARD, SOFT, PRIORITY:бакалавр, PRIORITY:работа, PRIORITY:магистратура, PRIORITY:проекты, PRIORITY:курсы",
                        "enum": [
                            "HARD",
                            "SOFT",
                            "PRIORITY:бакалавр",
                            "PRIORITY:работа",
                            "PRIORITY:магистратура",
                            "PRIORITY:проекты",
                            "PRIORITY:курсы",
                        ],
                    },
                    "recurrence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Правила повторения в формате RFC 5545, например ['RRULE:FREQ=WEEKLY;COUNT=9'] для 9 еженедельных повторений",
                    },
                    "reminder_minutes": {
                        "type": "integer",
                        "description": "Напоминание за X минут до события. Например 90 — за 1.5 часа. Если не указано — используется стандартное (30 мин).",
                    },
                },
                "required": ["title", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bulk_create_events",
            "description": (
                "Создать несколько событий за раз. Используй когда пользователь даёт "
                "недельное расписание или список занятий. "
                "ВСЕГДА требует подтверждения пользователя перед выполнением."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "events": {
                        "type": "array",
                        "description": "Список событий для создания",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Название события",
                                },
                                "start": {
                                    "type": "string",
                                    "description": "Начало ISO 8601, например '2026-03-24T09:30:00'",
                                },
                                "end": {
                                    "type": "string",
                                    "description": "Конец ISO 8601, например '2026-03-24T11:00:00'",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "Описание (опционально)",
                                },
                                "tag": {
                                    "type": "string",
                                    "description": "Тег события",
                                    "enum": [
                                        "HARD",
                                        "SOFT",
                                        "PRIORITY:бакалавр",
                                        "PRIORITY:работа",
                                        "PRIORITY:магистратура",
                                        "PRIORITY:проекты",
                                        "PRIORITY:курсы",
                                    ],
                                },
                                "recurrence": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Правила повторения RFC 5545, например ['RRULE:FREQ=WEEKLY;COUNT=9']",
                                },
                                "reminder_minutes": {
                                    "type": "integer",
                                    "description": "Напоминание за X минут до события.",
                                },
                            },
                            "required": ["title", "start", "end"],
                        },
                    }
                },
                "required": ["events"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_event",
            "description": (
                "Обновить или переместить существующее событие в Google Calendar. "
                "ВСЕГДА требует подтверждения пользователя перед выполнением. "
                "Никогда не трогай события с тегом [HARD]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID события из Google Calendar",
                    },
                    "event_title": {
                        "type": "string",
                        "description": "Название события (из предыдущего get_events) — только для отображения в диалоге подтверждения.",
                    },
                    "event_start": {
                        "type": "string",
                        "description": "Дата/время начала события ISO 8601 (из предыдущего get_events) — только для отображения в диалоге подтверждения.",
                    },
                    "fields": {
                        "type": "object",
                        "description": "Поля для обновления: title, start, end, description",
                        "properties": {
                            "title": {"type": "string"},
                            "start": {"type": "string"},
                            "end": {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                },
                "required": ["event_id", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": (
                "Удалить событие из Google Calendar. "
                "ВСЕГДА требует подтверждения пользователя перед выполнением. "
                "Никогда не удаляй события с тегом [HARD]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "ID события из Google Calendar",
                    },
                    "event_title": {
                        "type": "string",
                        "description": "Название события (из предыдущего get_events) — только для отображения в диалоге подтверждения.",
                    },
                    "event_start": {
                        "type": "string",
                        "description": "Дата/время начала события ISO 8601 (из предыдущего get_events) — только для отображения в диалоге подтверждения.",
                    },
                },
                "required": ["event_id"],
            },
        },
    },
    # -----------------------------------------------------------------------
    # Google Tasks
    # -----------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "get_tasks",
            "description": "Получить активные задачи из Google Tasks.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Создать новую задачу в Google Tasks. "
                "ВСЕГДА требует подтверждения пользователя перед выполнением."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Название задачи",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Время начала работы над задачей ISO 8601, например '2026-03-24T13:30:00'. Используй когда нужно заблокировать время в расписании.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "Время окончания работы над задачей ISO 8601. Обязательно если указан start_time.",
                    },
                    "due": {
                        "type": "string",
                        "description": "Срок сдачи/дедлайн в формате ISO 8601, например '2024-01-20T23:59:59'. Если пользователь не упомянул дедлайн — используй конец текущего дня. Если явно сказал что дедлайна нет — не передавай это поле.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Описание задачи (опционально)",
                    },
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "complete_task",
            "description": (
                "Отметить задачу как выполненную в Google Tasks. "
                "ВСЕГДА требует подтверждения пользователя перед выполнением. "
                "Передавай task_title — система сама найдёт задачу по названию."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_title": {
                        "type": "string",
                        "description": "Название задачи (полное или частичное). Используется для поиска задачи.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "ID задачи из результата get_tasks. Опционально — если передан, используется напрямую.",
                    },
                },
                "required": ["task_title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_task",
            "description": (
                "Удалить задачу из Google Tasks. "
                "ВСЕГДА требует подтверждения пользователя перед выполнением. "
                "Передавай task_title — система сама найдёт задачу по названию."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_title": {
                        "type": "string",
                        "description": "Название задачи (полное или частичное). Используется для поиска задачи.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "ID задачи из результата get_tasks. Опционально — если передан, используется напрямую.",
                    },
                },
                "required": ["task_title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_task",
            "description": (
                "Обновить задачу в Google Tasks (название, дедлайн, описание). "
                "ВСЕГДА требует подтверждения пользователя перед выполнением. "
                "Передавай task_title — система сама найдёт задачу по названию."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_title": {
                        "type": "string",
                        "description": "Название задачи (полное или частичное). Используется для поиска задачи.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "ID задачи из результата get_tasks. Опционально — если передан, используется напрямую.",
                    },
                    "fields": {
                        "type": "object",
                        "description": "Поля для обновления: title, due, description, start_time, end_time",
                        "properties": {
                            "title": {"type": "string"},
                            "due": {"type": "string"},
                            "description": {"type": "string"},
                            "start_time": {"type": "string"},
                            "end_time": {"type": "string"},
                        },
                    },
                },
                "required": ["task_title", "fields"],
            },
        },
    },
]
