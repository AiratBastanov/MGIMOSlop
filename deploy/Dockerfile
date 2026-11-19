FROM python:3.14.10-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements/ requirements/
RUN python -m pip install --no-cache-dir --only-binary=:all: -r requirements/render.txt && python -m pip check
COPY . .
# The reviewed Vite manifest, assets and fonts are shipped together in Git.
# Only this build step receives these inert settings. It never opens a database.
RUN DJANGO_SETTINGS_MODULE=deanery.settings.local DJANGO_SECRET_KEY=build-static-only \
    DB_NAME=unused DB_USER=unused DB_PASSWORD=unused python manage.py collectstatic --noinput \
    && useradd --uid 10001 --create-home deanery
USER deanery
ENV DJANGO_SETTINGS_MODULE=deanery.settings.production DEANERY_HOSTING=render
CMD ["sh", "-ec", "python manage.py migrate --check --noinput && exec gunicorn -c deploy/gunicorn.conf.py deanery.wsgi:application"]
