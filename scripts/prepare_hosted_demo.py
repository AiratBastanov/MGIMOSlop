"""Operator-only initialization or forward migration of a dedicated direct Neon database."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import django
from django.core.management import call_command
from django.db import connection
from accounts.private_files import demo_state_dir, protect_directory
from deanery.settings.neon import direct_database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, help='Точное имя выделенной демонстрационной БД.')
    parser.add_argument('--migrate-only', action='store_true', help='Только прямые миграции ранее подготовленной установки.')
    args = parser.parse_args()
    os.environ.update(DEANERY_HOSTING='render', DJANGO_SETTINGS_MODULE='deanery.settings.local',
                      MAIL_EXTERNAL_ENABLED='0', SUPPORT_PROVIDER='')
    config_file = ROOT / '.local' / 'hosted-demo' / 'service.json'
    saved = json.loads(config_file.read_text(encoding='utf-8')) if config_file.exists() else {}
    if args.migrate_only and not saved:
        parser.error('Для обновления нужна сохранённая конфигурация этой установки.')
    url = os.environ.get('DATABASE_URL') or saved.get('DATABASE_URL')
    if not url:
        if not sys.stdin.isatty():
            parser.error('Введите прямой URI в интерактивном терминале или передайте через защищённое окружение.')
        url = getpass.getpass('Прямой URI новой БД Neon (ввод скрыт): ')
    database = direct_database(url)
    if database['NAME'] != args.database:
        parser.error('Имя в URI не совпало с --database.')
    if saved and saved['DATABASE_URL'] != url:
        parser.error('Каталог уже привязан к другому URI. Проверьте закрытую конфигурацию установки.')
    os.environ['DATABASE_URL'] = url
    os.environ['DJANGO_SECRET_KEY'] = saved['DJANGO_SECRET_KEY'] if saved else secrets.token_urlsafe(60)
    django.setup()
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT current_database(), current_user, current_schema(), pg_try_advisory_lock(749240118)')
            name, user, schema, locked = cursor.fetchone()
            if (name, user, schema) != (args.database, database['USER'], 'public') or not locked:
                parser.error('БД, роль, схема или блокировка операции не подтверждены.')
            cursor.execute("""SELECT r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls
                                     OR EXISTS (SELECT 1 FROM pg_roles n WHERE n.rolname='neon_superuser'
                                                AND pg_has_role(r.oid, n.oid, 'MEMBER'))
                              FROM pg_roles r WHERE r.rolname=current_user""")
            if cursor.fetchone()[0]:
                parser.error('Приложению нужна отдельная роль БД без административных прав.')
            cursor.execute("SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'btree_gist')")
            if not cursor.fetchone()[0]:
                parser.error('В этой БД недоступно обязательное расширение btree_gist.')
            if not saved:
                cursor.execute("""SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                                  WHERE n.nspname='public' AND c.relkind IN ('r','p','v','m','S','f'))""")
                if cursor.fetchone()[0]:
                    parser.error('Первая подготовка требует пустой новой БД. Миграции не выполнялись.')
                protect_directory(demo_state_dir())
                if (demo_state_dir() / 'demo-credentials.json').exists():
                    parser.error('В каталоге уже есть учётные данные другой установки.')
                with config_file.open('x', encoding='utf-8') as stream:
                    json.dump({key: os.environ[key] for key in ('DATABASE_URL', 'DJANGO_SECRET_KEY')}, stream)
        call_command('migrate', interactive=False)
        if not args.migrate_only:
            call_command('prepare_demo', database=args.database)
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database()), extversion FROM pg_extension WHERE extname='btree_gist'")
            size, extension = cursor.fetchone()
        print(f'БД {name}, btree_gist {extension}. Занято {size} байт.')
    finally:
        connection.close()  # Releases the session-level operator lock on every exit.


if __name__ == '__main__':
    main()
