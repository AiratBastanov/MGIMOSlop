"""Direct Neon connection for the web service and explicit operator commands."""
import os
import re

import certifi
from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict


def direct_database(url):
    message = 'Нужен прямой PostgreSQL URI Neon с именем БД, пользователем и паролем.'
    if not url.startswith(('postgresql://', 'postgres://')):
        raise ValueError(message)
    try:
        fields = conninfo_to_dict(url)
    except ProgrammingError:
        raise ValueError(message) from None
    host = fields.get('host', '')
    if (not re.fullmatch(r'ep-[a-z0-9-]+(?:\.[a-z0-9-]+)+\.neon\.tech', host)
            or host.split('.')[0].endswith('-pooler')
            or not all(fields.get(key) for key in ('dbname', 'user', 'password'))
            or not re.fullmatch(r'[\w-]{1,63}', fields['dbname'])
            or fields.get('port', '5432') != '5432'
            or fields.get('sslmode', 'require') not in ('require', 'verify-ca', 'verify-full')
            or fields.get('channel_binding', 'require') not in ('prefer', 'require')
            or fields.keys() - {'host', 'port', 'user', 'password', 'dbname', 'sslmode', 'channel_binding'}
            or os.environ.get('PGHOSTADDR') or os.environ.get('PGSERVICE')):
        raise ValueError(message)
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'HOST': host, 'PORT': '5432', 'NAME': fields['dbname'],
        'USER': fields['user'], 'PASSWORD': fields['password'],
        'CONN_MAX_AGE': 30, 'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {
            'sslmode': 'verify-full', 'sslrootcert': certifi.where(), 'channel_binding': 'require',
            'connect_timeout': 15, 'options': '-c statement_timeout=10000 -c lock_timeout=5000',
        },
    }
