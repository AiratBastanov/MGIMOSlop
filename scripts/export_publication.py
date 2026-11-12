"""Copy only the reviewed include manifest; never copy a development Git history."""
import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def selected_files(root=ROOT):
    spec = json.loads((root / 'scripts/publication-files.json').read_text(encoding='utf-8'))
    paths = {p for pattern in spec['include'] for p in root.glob(pattern) if p.is_file()}
    return sorted(p for p in paths if not any(fnmatch.fnmatch(p.relative_to(root).as_posix(), pattern) for pattern in spec['exclude']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--receipt', required=True, help='Private mapping outside the publication tree.')
    args = parser.parse_args()
    destination = Path(args.destination).resolve()
    receipt = Path(args.receipt).resolve()
    if destination == ROOT or ROOT.is_relative_to(destination) or receipt.is_relative_to(destination):
        parser.error('Нужна отдельная папка публикации и внешняя карта файлов.')
    source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    spec = json.loads((ROOT / 'scripts/publication-files.json').read_text(encoding='utf-8'))
    destination.mkdir(parents=True, exist_ok=True)
    old = json.loads(receipt.read_text(encoding='utf-8')) if receipt.exists() else {}
    if old and old['destination'] != str(destination):
        parser.error('Карта принадлежит другой папке.')
    files = {}
    for source in selected_files():
        rel = source.relative_to(ROOT).as_posix()
        target = destination / rel
        if not target.resolve().is_relative_to(destination):
            parser.error('Путь выходит из папки публикации.')
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel == 'AGENTS.md':
            target.write_text('\n'.join(spec['public_instructions']), encoding='utf-8', newline='\n')
        else:
            shutil.copyfile(source, target)
        files[rel] = {'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'bytes': target.stat().st_size}
    for rel in old.get('files', {}).keys() - files.keys():
        target = (destination / rel).resolve()
        if not target.is_relative_to(destination):
            parser.error('Недопустимый старый путь.')
        if target.is_file():
            target.unlink()
    result = {'source_commit': source_commit,
              'destination': str(destination), 'files': files, 'count': len(files),
              'bytes': sum(item['bytes'] for item in files.values())}
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"Подготовлено файлов: {result['count']}; байтов: {result['bytes']}. История разработки не копировалась.")


if __name__ == '__main__':
    main()
