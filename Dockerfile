FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

# libcairo2 is needed by cairosvg to render SVG logos
RUN apt-get update \
    && apt-get install -y --no-install-recommends libcairo2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# collectstatic needs a key but never uses it for anything persistent
RUN SECRET_KEY=build-only DEBUG=False python manage.py collectstatic --noinput

RUN useradd --create-home --uid 1000 app \
    && mkdir -p /data/media \
    && chown -R app:app /data
USER app

VOLUME ["/data"]
EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn chest.wsgi:application --bind 0.0.0.0:8000 --workers ${GUNICORN_WORKERS:-3} --timeout 60"]
