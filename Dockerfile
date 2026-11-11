# API and worker image (same image, different command)
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY Code/requirements.txt Code/requirements.txt
RUN pip install --no-cache-dir -r Code/requirements.txt
COPY Code/ Code/
WORKDIR /app/Code
RUN useradd -m appuser && mkdir -p /app/data/uploads && chown -R appuser /app/data
USER appuser
EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
