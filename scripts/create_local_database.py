"""Create one new local PostgreSQL database and restricted role, then protect config."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import sys

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from accounts.private_files import protect_directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='deanery_demo')
    parser.add_argument('--port', type=int, default=5548)
    parser.add_argument('--admin', default='postgres')
    parser.add_argument('--config', default='.local/app.json')
    args = parser.parse_args()
    if not re.fullmatch(r'deanery_[a-z0-9_]{1,45}', args.database):
        parser.error('Имя новой БД должно начинаться с deanery_ и содержать строчные буквы, цифры или подчёркивания.')
    path = (ROOT / args.config).resolve()
    if not path.is_relative_to(ROOT / '.local') or path.suffix != '.json':
        parser.error('Конфигурация должна быть JSON-файлом внутри .local этого проекта.')
    if path.exists():
        config = json.loads(path.read_text(encoding='utf-8'))
        if config.get('DB_NAME') != args.database or str(config.get('DB_PORT')) != str(args.port):
            parser.error('Существующий файл относится к другой БД или порту. Он сохранён.')
        with psycopg.connect(host=config['DB_HOST'], port=config['DB_PORT'], dbname=config['DB_NAME'],
                            user=config['DB_USER'], password=config['DB_PASSWORD'], connect_timeout=3):
            pass
        print('Конфигурация и БД уже существуют; пароль и данные сохранены.')
        return
    protect_directory(path.parent)
    password = os.environ.get('DEANERY_PG_ADMIN_PASSWORD') or getpass.getpass('Пароль локального администратора PostgreSQL: ')
    config = {'DJANGO_SECRET_KEY': secrets.token_urlsafe(64), 'DB_NAME': args.database,
              'DB_USER': args.database, 'DB_PASSWORD': secrets.token_urlsafe(40), 'DB_HOST': '127.0.0.1',
              'DB_PORT': str(args.port), 'PUBLIC_BASE_URL': 'http://127.0.0.1:8000',
              'SUPPORT_PROVIDER': '', 'SUPPORT_MODEL': '', 'SUPPORT_LOCAL_ONLY_CONFIRMED': '0'}
    with psycopg.connect(host='127.0.0.1', port=args.port, dbname='postgres', user=args.admin,
                         password=password, connect_timeout=3, autocommit=True) as conn:
        if conn.execute('SELECT 1 FROM pg_database WHERE datname=%s', [args.database]).fetchone() or conn.execute(
                'SELECT 1 FROM pg_roles WHERE rolname=%s', [args.database]).fetchone():
            parser.error('БД или роль уже существует без этого config. Пароли не сбрасываются; выберите новое имя или исходную конфигурацию.')
        pending = path.with_suffix('.pending.json')
        with pending.open('x', encoding='utf-8') as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
        # CREATE DATABASE cannot run inside a transaction. Pending config preserves
        # the new password if an administrator must resolve an interrupted creation.
        conn.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD {}').format(
            sql.Identifier(args.database), sql.Literal(config['DB_PASSWORD'])))
        conn.execute(sql.SQL('CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING {}').format(
            sql.Identifier(args.database), sql.Identifier(args.database), sql.Literal('UTF8')))
        conn.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(args.database)))
        conn.execute(sql.SQL('GRANT CONNECT, TEMPORARY ON DATABASE {} TO {}').format(sql.Identifier(args.database), sql.Identifier(args.database)))
        pending.replace(path)
    print('Созданы отдельная пустая БД и роль без административных прав.')
    print('Защищённая конфигурация:', path.relative_to(ROOT).as_posix())


if __name__ == '__main__':
    try:
        main()
    except psycopg.Error:
        raise SystemExit('PostgreSQL не подтвердил операцию. Проверьте порт/права. Если появился .pending.json, сохраните его для администратора; автоматического reset нет.')
