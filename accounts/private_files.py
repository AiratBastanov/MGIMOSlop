"""Local installation files: protect the directory before writing any secret."""
import os
from pathlib import Path
import subprocess
from django.conf import settings


def demo_state_dir():
    root = settings.BASE_DIR / '.local'
    return root / 'hosted-demo' if settings.RENDER_HOSTING else root


def protect_directory(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True).strip().split(',')[-1].strip('"')
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
                        f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F'],
                       check=True, capture_output=True)
    else:
        path.chmod(0o700)
    return path
