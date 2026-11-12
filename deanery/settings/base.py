import json
import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
config_path = Path(os.environ.get('DEANERY_CONFIG', BASE_DIR / '.local' / 'app.json'))
config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}

def setting(name, default=None):
    value = os.environ.get(name, config.get(name, default))
    if value is None:
        raise RuntimeError(f'Не задан параметр {name}; см. README.md')
    return value

SECRET_KEY = setting('DJANGO_SECRET_KEY')
DEBUG = False
ALLOWED_HOSTS = setting('DJANGO_ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')
INSTALLED_APPS = [
    'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions',
    'django.contrib.staticfiles', 'data_import', 'studies', 'notifications.apps.NotificationsConfig', 'accounts', 'student_requests', 'materials', 'profiles', 'workspaces', 'axes',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'accounts.middleware.ResponsePolicyMiddleware',
    'accounts.middleware.GuardedSessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'materials.uploads.MaterialUploadMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'axes.middleware.AxesMiddleware',
]
ROOT_URLCONF = 'deanery.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
              'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
              'OPTIONS': {'context_processors': ['django.template.context_processors.request']}}]
WSGI_APPLICATION = 'deanery.wsgi.application'
DATABASES = {'default': {
    'ENGINE': 'django.db.backends.postgresql',
    'NAME': setting('DB_NAME'), 'USER': setting('DB_USER'),
    'PASSWORD': setting('DB_PASSWORD'), 'HOST': setting('DB_HOST', '127.0.0.1'),
    'PORT': setting('DB_PORT', '55432'), 'CONN_MAX_AGE': 0,
    'OPTIONS': {'connect_timeout': 3, 'options': '-c statement_timeout=10000 -c lock_timeout=5000'},
    'TEST': {'NAME': 'deanery_test'},
}}
LANGUAGE_CODE = 'ru-ru'
TIME_ZONE = 'Europe/Moscow'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
X_FRAME_OPTIONS = 'DENY'
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
SESSION_COOKIE_AGE = 7200
SESSION_SAVE_EVERY_REQUEST = False
CSRF_COOKIE_SAMESITE = 'Lax'
CSRF_FAILURE_VIEW = 'accounts.views.csrf_failure'
AUTHENTICATION_BACKENDS = ['axes.backends.AxesStandaloneBackend','django.contrib.auth.backends.ModelBackend']
AXES_HANDLER = 'axes.handlers.database.AxesDatabaseHandler'
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=15)
AXES_LOCKOUT_PARAMETERS = [['username','ip_address']]
AXES_USE_ATTEMPT_EXPIRATION = False
AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT = False
AXES_RESET_ON_SUCCESS = True
AXES_CLIENT_IP_CALLABLE = 'accounts.access.client_ip'
# Local/direct mode never trusts visitor-supplied headers.
CLIENT_IP_TRUSTED_PROXY = ''
DATA_UPLOAD_MAX_MEMORY_SIZE = 4 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
MATERIAL_ATTACHMENT_COUNT = min(5, max(1, int(setting('MATERIAL_ATTACHMENT_COUNT', '5'))))
MATERIAL_ATTACHMENT_FILE_BYTES = min(10*1024*1024, max(1, int(setting('MATERIAL_ATTACHMENT_FILE_BYTES', str(10*1024*1024)))))
MATERIAL_ATTACHMENT_TOTAL_BYTES = min(25*1024*1024, max(1, int(setting('MATERIAL_ATTACHMENT_TOTAL_BYTES', str(25*1024*1024)))))
SUPPORT_PROVIDER = setting('SUPPORT_PROVIDER', '')
SUPPORT_ENDPOINT = setting('SUPPORT_ENDPOINT', 'http://127.0.0.1:11434')
SUPPORT_MODEL = setting('SUPPORT_MODEL', '')
SUPPORT_LOCAL_ONLY_CONFIRMED = str(setting('SUPPORT_LOCAL_ONLY_CONFIRMED', '0')) == '1'
# Capture backend is the default even if SMTP credentials happen to be present.
# External sends require the separate explicit MAIL_EXTERNAL_ENABLED switch.
EMAIL_BACKEND = setting('EMAIL_BACKEND', 'notifications.mail_backend.CaptureBackend')
EMAIL_FILE_PATH = BASE_DIR / '.local' / 'captured-email'
EMAIL_HOST = setting('EMAIL_HOST', '')
EMAIL_PORT = int(setting('EMAIL_PORT', '587'))
EMAIL_HOST_USER = setting('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = setting('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = setting('EMAIL_USE_TLS', '1') == '1'
EMAIL_TIMEOUT = 15
DEFAULT_FROM_EMAIL = setting('DEFAULT_FROM_EMAIL', 'cabinet@example.invalid')
MAIL_EXTERNAL_ENABLED = setting('MAIL_EXTERNAL_ENABLED', '0') == '1'
PUBLIC_BASE_URL = setting('PUBLIC_BASE_URL', 'http://127.0.0.1:8000')
AXES_LOCKOUT_CALLABLE = 'accounts.views.locked_out'
AXES_DISABLE_ACCESS_LOG = True
AXES_ENABLE_ACCESS_FAILURE_LOG = False
AXES_SENSITIVE_PARAMETERS = ['username','ip_address','user_agent','csrfmiddlewaretoken','context']
LOGGING = {'version':1,'disable_existing_loggers':False,
           'handlers':{'null':{'class':'logging.NullHandler'}},
           'loggers':{'axes':{'handlers':['null'],'propagate':False}}}
