from .base import *  # noqa: F403

# Single on-host Caddy -> IPv4 loopback Gunicorn only. No CIDRs or proxy chains.
CLIENT_IP_TRUSTED_PROXY = setting('DJANGO_CLIENT_IP_TRUSTED_PROXY', '')  # noqa: F405
if CLIENT_IP_TRUSTED_PROXY not in ('', '127.0.0.1'):
    raise RuntimeError('DJANGO_CLIENT_IP_TRUSTED_PROXY: допустимы пустое значение или 127.0.0.1')

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
CSRF_TRUSTED_ORIGINS = setting('DJANGO_CSRF_TRUSTED_ORIGINS').split(',')  # noqa: F405
# Start with one hour; includeSubDomains/preload require a separate domain-wide decision.
SECURE_HSTS_SECONDS = int(setting('DJANGO_SECURE_HSTS_SECONDS', '3600'))  # noqa: F405
