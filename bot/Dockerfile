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

# Директория для данных
RUN mkdir -p data/backups

# Запуск
CMD ["python", "main.py"]
