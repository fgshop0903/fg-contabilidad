FROM python:3.12-slim

# Evitar archivos .pyc y activar logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Instalar dependencias del sistema para MySQL y Gráficos
RUN apt-get update && apt-get install -y \
    build-essential \
    libmariadb-dev \
    pkg-config \
    libcairo2-dev \
    libjpeg-dev \
    libgif-dev \
    libpango1.0-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalamos librerías básicas necesarias
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt whitenoise gunicorn

COPY . /app/

EXPOSE 8001

CMD ["gunicorn", "--bind", "0.0.0.0:8001", "config.wsgi:application"]