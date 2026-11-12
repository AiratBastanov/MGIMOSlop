"""Render the editable document with local fonts; reject missing assets/overflow."""
import argparse
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    if output.exists():
        parser.error('Файл уже существует: выберите новый путь, затем просмотрите результат.')
    output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        try:
            page = browser.new_page(viewport={'width': 1100, 'height': 1200})
            page.goto((ROOT / 'docs/creation-process.html').as_uri(), wait_until='load')
            page.emulate_media(media='print')
            page.evaluate("async () => { await document.fonts.load('14px Golos', 'Текст'); await document.fonts.load('600 14px Golos', 'Текст'); await document.fonts.ready; }")
            checks = page.evaluate('''() => ({
                font: document.fonts.check('14px Golos', 'Текст') && [...document.fonts].every(f => f.status === 'loaded'),
                images: [...document.images].every(i => i.complete && i.naturalWidth > 0),
                overflow: [...document.querySelectorAll('.page-body')].filter(e => e.scrollHeight > e.clientHeight + 1).map(e => e.parentElement.id),
                pages: document.querySelectorAll('.sheet').length,
                localLinks: [...document.querySelectorAll('a[href]')].filter(a => a.getAttribute('href').startsWith('#') && !document.getElementById(a.hash.slice(1))).length
            })''')
            if not checks['font'] or not checks['images'] or checks['overflow'] or checks['localLinks']:
                raise RuntimeError('Документ не прошёл проверку компоновки: ' + str(checks))
            page.pdf(path=str(output), prefer_css_page_size=True, print_background=True, tagged=True, outline=True)
            print(f"PDF создан: {checks['pages']} страниц. Просмотрите каждую страницу перед передачей.")
        finally:
            browser.close()


if __name__ == '__main__':
    main()
