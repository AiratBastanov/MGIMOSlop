"""Production manifest only: no Vite server, CDN or inline executable script."""
import json
from pathlib import PurePosixPath
from django import template
from django.conf import settings
from django.templatetags.static import static
from django.utils.html import format_html, format_html_join

register = template.Library()

@register.simple_tag
def frontend_assets():
    path = settings.BASE_DIR / 'static/frontend/manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    seen = set()
    tags = []
    def asset(name):
        if name.startswith('/') or '..' in PurePosixPath(name).parts:
            raise ValueError('Unsafe frontend manifest path')
        return static('frontend/' + name)
    def visit(key):
        if key in seen:
            return
        seen.add(key)
        entry = manifest[key]
        for dependency in entry.get('imports', []):
            visit(dependency)
        for css in entry.get('css', []):
            tags.append(format_html('<link rel="stylesheet" href="{}">', asset(css)))
        if key != 'src/main.tsx':
            tags.append(format_html('<link rel="modulepreload" href="{}">', asset(entry['file'])))
    visit('src/main.tsx')
    tags.append(format_html('<script type="module" src="{}"></script>', asset(manifest['src/main.tsx']['file'])))
    return format_html_join('\n', '{}', ((tag,) for tag in tags))
