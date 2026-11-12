from .base import *  # noqa: F403

# Только локальная разработка; runserver --insecure раздаёт собственную статику.
DEBUG = False

# Operator-only response header fingerprints the production frontend loaded at startup.
import hashlib
_manifest = BASE_DIR / 'static/frontend/manifest.json'  # noqa: F405
DEVELOPMENT_BUILD = 'release.' + hashlib.sha256(_manifest.read_bytes()).hexdigest()[:12] if _manifest.exists() else 'release.build-missing'
