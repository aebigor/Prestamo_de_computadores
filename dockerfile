# ─── Imagen base ──────────────────────────────────────────────
FROM python:3.11-slim

# Variables de entorno
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

# ─── Directorio de trabajo ─────────────────────────────────────
WORKDIR /app

# ─── Dependencias del sistema (para Pillow y barcode) ──────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libzbar0 \
    && rm -rf /var/lib/apt/lists/*

# ─── Dependencias Python ───────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── Código de la aplicación ───────────────────────────────────
COPY main.py .

# ─── Carpetas persistentes ─────────────────────────────────────
# uploads/ y static/ se crean en runtime por main.py,
# pero las declaramos para que Docker las reconozca como volúmenes
RUN mkdir -p uploads static

# ─── Copiar el frontend al directorio static ──────────────────
COPY static/index.html static/index.html

# ─── Render usa la variable PORT automáticamente ──────────────
EXPOSE $PORT

# ─── Arrancar con uvicorn ──────────────────────────────────────
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]