from .base import *  # noqa: F403

if HOSTING_PROVIDER:
    from deanery.hosting import assigned_hostname
    if len(SECRET_KEY) < 50:
        raise RuntimeError('Требуется постоянный случайный DJANGO_SECRET_KEY не короче 50 символов.')
    host = assigned_hostname(HOSTING_PROVIDER)
    ALLOWED_HOSTS = [host]
    PUBLIC_BASE_URL = 'https://' + host
    CSRF_TRUSTED_ORIGINS = [PUBLIC_BASE_URL]
    MIDDLEWARE = ['deanery.ingress.hosted_https', MIDDLEWARE[0],
                  'whitenoise.middleware.WhiteNoiseMiddleware', *MIDDLEWARE[1:]]
    WHITENOISE_IMMUTABLE_FILE_TEST = r'^/static/frontend/assets/[^/]+-[A-Za-z0-9_-]{8}\.[^/.]+$'
    WHITENOISE_MAX_AGE = 0
    WHITENOISE_ALLOW_ALL_ORIGINS = False
    # Public visitor addresses are not authenticated at the application boundary.
    AXES_LOCKOUT_PARAMETERS = ['username']
    SECURE_PROXY_SSL_HEADER = None
    SUPPORT_PROVIDER = SUPPORT_MODEL = ''
    SUPPORT_LOCAL_ONLY_CONFIRMED = MAIL_EXTERNAL_ENABLED = False
    EMAIL_BACKEND = 'notifications.mail_backend.CaptureBackend'
else:
    # Single on-host Caddy -> IPv4 loopback Gunicorn only.
    CLIENT_IP_TRUSTED_PROXY = setting('DJANGO_CLIENT_IP_TRUSTED_PROXY', '')
    if CLIENT_IP_TRUSTED_PROXY not in ('', '127.0.0.1'):
        raise RuntimeError('DJANGO_CLIENT_IP_TRUSTED_PROXY: допустимы пустое значение или 127.0.0.1')
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    CSRF_TRUSTED_ORIGINS = setting('DJANGO_CSRF_TRUSTED_ORIGINS').split(',')

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
# Start with one hour; includeSubDomains/preload require a separate domain-wide decision.
SECURE_HSTS_SECONDS = int(setting('DJANGO_SECURE_HSTS_SECONDS', '3600'))  # noqa: F405
