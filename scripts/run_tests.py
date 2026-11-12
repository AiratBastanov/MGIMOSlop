"""Отдельная тестовая БД без PowerShell execution-policy изменений."""
from pathlib import Path
import os
import subprocess
import sys
import argparse

root = Path(__file__).resolve().parents[1]
env = {**os.environ,'DEANERY_CONFIG':str(root / '.local/test.json'),
       'DJANGO_SETTINGS_MODULE':'deanery.settings.testing','PYTHONIOENCODING':'utf-8'}
parser = argparse.ArgumentParser()
parser.add_argument('--log')
parser.add_argument('--label', action='append', help='Метка теста; можно повторить')
args, remaining = parser.parse_known_args()
command = [sys.executable,'manage.py','test',*(args.label or ['tests']),'--verbosity','2',*remaining]
if args.log:
    log = Path(args.log)
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists():
        parser.error('Файл результата уже существует; укажите новый путь --log')
    result = subprocess.run(command,cwd=root,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    with log.open('xb') as output:
        output.write(result.stdout)
    sys.stdout.buffer.write(result.stdout)
    raise SystemExit(result.returncode)
raise SystemExit(subprocess.call(command,cwd=root,env=env))
