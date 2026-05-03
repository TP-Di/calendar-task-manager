FROM python:3.11-slim

# Рабочая директория
WORKDIR /app

# Системные зависимости для pdfplumber
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpoppler-cpp-dev \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Создаём непривилегированного пользователя
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 botuser

# Копируем зависимости и устанавливаем
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY . .

# Директория для данных
RUN mkdir -p data/backups && chown -R botuser:botuser /app

USER botuser

# Запуск
CMD ["python", "main.py"]
