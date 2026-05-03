FROM python:3.11-slim

# Рабочая директория
WORKDIR /app

# Системные зависимости для pdfplumber
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpoppler-cpp-dev \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Копируем зависимости и устанавливаем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Директория для данных (SQLite + бэкапы + runtime.env)
RUN mkdir -p data/backups

# Запуск под root: bind-mount .env и named volume data/ должны быть writable
# для процесса бота. Non-root user приводил к permission denied на хостовых
# файлах (uid mismatch). Контейнер изолирован — root внутри ≠ root на хосте.
CMD ["python", "main.py"]
