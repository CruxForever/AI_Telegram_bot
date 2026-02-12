# Используем официальный образ AWS Lambda Python 3.11
FROM public.ecr.aws/lambda/python:3.11

# Устанавливаем pip и обновляем его
RUN pip install --upgrade pip

# Копируем requirements.txt
COPY requirements.txt .

# Устанавливаем зависимости в /opt/python (структура для Lambda Layer)
RUN pip install -r requirements.txt -t /opt/python

# Очищаем кеш и временные файлы для уменьшения размера layer
RUN find /opt/python -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
RUN find /opt/python -type d -name "*.dist-info" -exec rm -rf {}/RECORD {} + 2>/dev/null || true
RUN find /opt/python -type d -name "tests" -exec rm -rf {} + 2>/dev/null || true
RUN find /opt/python -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true

# По умолчанию просто показываем содержимое (для отладки)
CMD ["ls", "-la", "/opt/python"]
