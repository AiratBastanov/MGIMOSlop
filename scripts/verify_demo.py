"""Browser verification of a local or explicitly selected hosted synthetic demo."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deanery.hosting import assigned_hostname


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--credentials')
    parser.add_argument('--hosted', action='store_true', help='Проверить точный HTTPS-домен выбранной площадки.')
    parser.add_argument('--after-restart', type=Path, help='Повторно прочитать заявку, отзыв и файлы по предыдущему checks.json.')
    parser.add_argument('--output', required=True)
    parser.add_argument('--read-only', action='store_true', help='Only login/read/helper; no new request or review.')
    parser.add_argument('--provider-state', choices=('disabled', 'unavailable'), default='disabled')
    args = parser.parse_args()
    credentials_path = Path(args.credentials or '.local/demo-credentials.json')
    if args.hosted:
        try:
            host = assigned_hostname(os.environ.get('DEANERY_HOSTING', 'render'))
        except ValueError as error:
            parser.error(str(error))
        if args.base_url != 'https://' + host:
            parser.error('Нужен точный HTTPS-адрес выбранного сервиса.')
        if args.credentials:
            parser.error('Для внешнего сервиса используются только учётные данные выделенной установки.')
        credentials_path = ROOT / '.local/hosted-demo/demo-credentials.json'
        expect.set_options(timeout=20000)
    elif urlsplit(args.base_url).hostname not in ('127.0.0.1', 'localhost'):
        parser.error('Проверка предназначена только для локального демо.')
    previous = None
    if args.after_restart:
        previous = json.loads(args.after_restart.read_text(encoding='utf-8'))
        if previous['origin'] != args.base_url or not {'request', 'material', 'files'} <= previous['saved'].keys():
            parser.error('Нужен результат полного первого прогона на этом же адресе.')
        args.read_only = True
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    credentials = json.loads(credentials_path.read_text(encoding='utf-8'))
    facts = {'origin': args.base_url, 'checks': [], 'saved': {}, 'screenshots': []}

    def fact(name):
        facts['checks'].append(name)
        (out / 'checks.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding='utf-8')
        print(name, flush=True)

    def ready(page):
        expect(page.locator('#private-content:not([inert])')).to_be_visible()
        expect(page.locator('#privacy-state')).to_be_hidden()
        assert page.evaluate('window.deanery.active')
        for el in page.locator('[data-component]').all():
            expect(el).not_to_be_empty()
        page.evaluate('document.fonts.ready')

    def goto(page, path):
        response = page.goto(args.base_url + path)
        assert response.status == 200, (path, response.status)
        ready(page)

    def login(page, user):
        route = '/schedule/?date=2026-11-23&view=week' if user == 'student_a' else '/login/'
        page.goto(args.base_url + route)
        login_url = urlsplit(page.url)
        assert login_url[:2] == urlsplit(args.base_url)[:2] and login_url.path == '/login/'
        page.get_by_label('Логин', exact=True).fill(user)
        page.get_by_label('Пароль', exact=True).fill(credentials[user])
        page.get_by_role('button', name='Войти', exact=False).click()
        ready(page)
        if user == 'student_a':
            expect(page).to_have_url(args.base_url + route)

    def capture(page, name):
        page.screenshot(path=str(out / name))
        facts['screenshots'].append(name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='chrome', headless=True)
        facts['browser'] = browser.version
        contexts, pages = {}, {}
        try:
            for user in ('student_a', 'student_b', 'teacher_demo', 'staff_demo'):
                context = browser.new_context(viewport={'width': 1366, 'height': 900}, locale='ru-RU', timezone_id='Europe/Moscow')
                page = context.new_page()
                login(page, user)
                contexts[user], pages[user] = context, page
                assert context.request.get(args.base_url + '/health/ready/').status == 200
                page.get_by_role('button', name='Помощник', exact=True).click()
                expect(page.locator('.help-suggestions button').first).to_be_visible()
                expect(page.locator('.assistant-mode')).to_contain_text('ИИ не подключён', timeout=7000)
                with page.expect_response(lambda r: r.url.endswith('/support/') and r.request.method == 'POST') as response:
                    page.locator('.help-suggestions button').first.click()
                data = response.value.json()
                assert data['mode'] == 'faq' and data['provider']['state'] == args.provider_state
                page.get_by_role('button', name='Закрыть', exact=True).click()
                fact(user + ': вход, готовность приложения и готовая справка без ИИ')

            s, b, t, f = (pages[name] for name in ('student_a', 'student_b', 'teacher_demo', 'staff_demo'))
            goto(s, '/today/'); capture(s, 'student-today.png')
            goto(s, '/study/'); capture(s, 'student-studies.png')
            goto(s, '/study/?view=results&period=all&filter=assessed')
            goto(s, '/notifications/')
            s.locator('.header-search').click(); ready(s)
            s.locator('#cabinet-search').fill('Расписание')
            s.get_by_role('button', name='Найти', exact=True).click()
            s.locator('#search-results a[href="/schedule/"]').click(); ready(s)
            expect(s).to_have_url(args.base_url + '/schedule/')
            fact('Глубокая ссылка после входа, оценки, уведомления и переход к расписанию из поиска')
            goto(s, '/schedule/?date=2026-11-23&view=week')
            expect(s.locator('main')).to_contain_text('2026')
            goto(b, '/profile/'); expect(b.locator('main')).to_contain_text('Тимур Светлов')
            goto(s, '/profile/group/'); expect(s.locator('main')).to_contain_text('Дарья Волкова')
            goto(t, '/teacher/'); capture(t, 'teacher-workspace.png')
            goto(f, '/staff/'); capture(f, 'faculty-workspace.png')
            goto(s, '/study/teaching/')
            s.get_by_role('link', name='[Демо] Материал с двумя файлами', exact=True).click(); ready(s)
            files = s.locator('.publication-content .attachment-list a')
            assert files.count() == 2
            fingerprints = {}
            for link in files.all():
                path = link.get_attribute('href')
                url = args.base_url + path
                response = contexts['student_a'].request.get(url)
                assert response.status == 200 and response.body()
                fingerprints[path] = hashlib.sha256(response.body()).hexdigest()
                assert contexts['student_b'].request.get(url).status == 404
            facts['saved']['files'] = fingerprints
            fact('Учебные экраны, отдельные профили, группа и два приватных файла преподавателя')

            # Every built asset is served by Django, not a separate Vite process.
            assets = []
            for item in json.loads((ROOT / 'static/frontend/manifest.json').read_text(encoding='utf-8')).values():
                assets.extend([item['file'], *item.get('css', []), *item.get('assets', [])])
            for asset in set(assets):
                response = contexts['student_a'].request.get(args.base_url + '/static/frontend/' + asset)
                assert response.status == 200
                assert hashlib.sha256(response.body()).digest() == hashlib.sha256((ROOT / 'static/frontend' / asset).read_bytes()).digest()
            fact('Готовые JS, CSS и шрифты совпадают с опубликованной сборкой')

            if previous:
                saved = previous['saved']
                assert saved['files'] == fingerprints
                goto(s, f"/requests/{int(saved['request'])}/")
                expect(s.locator('.request-current-status')).to_have_text('Готова')
                expect(s.locator('main')).to_contain_text('Демонстрация завершена.')
                goto(s, f"/study/materials/{int(saved['material'])}/")
                expect(s.locator('.material-feedback').first).to_contain_text('Аргумент и примеры согласованы.')
                fact('После рестарта сохранились четыре пароля, ответ службы, отзыв и SHA-256 двух файлов')

            if not args.read_only:
                goto(s, '/requests/new/')
                s.get_by_label('Назначение', exact=True).select_option('other')
                s.get_by_label('Куда требуется справка', exact=True).fill('Для учебной демонстрации')
                s.get_by_label('Количество экземпляров', exact=True).fill('2')
                s.get_by_role('button', name='Отправить заявку', exact=True).click(); ready(s)
                expect(s.locator('.request-current-status')).to_be_visible()
                qid = int(s.url.rstrip('/').split('/')[-1]); facts['saved']['request'] = qid
                goto(f, f'/staff/requests/{qid}/')
                f.get_by_role('button', name='Взять в работу', exact=True).click()
                expect(f.locator('.request-current-status')).to_have_text('В работе')
                f.get_by_label('Что нужно уточнить', exact=True).fill('Подтвердите цель использования документа.')
                f.get_by_role('button', name='Запросить уточнение', exact=True).click()
                expect(f.locator('#clarification')).to_contain_text('Ожидаем уточнение')
                goto(s, f'/requests/{qid}/')
                s.get_by_label('Ваш ответ', exact=True).fill('Документ нужен для демонстрации работы кабинета.')
                s.get_by_role('button', name='Отправить ответ', exact=True).click()
                expect(s.locator('#clarification')).to_contain_text('Ответ получен')
                goto(f, f'/staff/requests/{qid}/')
                f.get_by_role('button', name='Уточнение рассмотрено', exact=True).click()
                f.get_by_label('Пояснение демонстрационного результата', exact=True).fill('Демонстрация завершена. Действительная справка не выпускается.')
                f.get_by_role('button', name='Завершить обработку', exact=True).click()
                expect(f.locator('.request-current-status')).to_have_text('Готова')
                goto(s, f'/requests/{qid}/')
                expect(s.locator('.request-current-status')).to_have_text('Готова')
                assert contexts['student_b'].request.get(args.base_url + f'/requests/{qid}/').status == 404
                capture(s, 'request-decision.png')
                fact('Сохранённый обмен студент — служба: заявка, уточнение, ответ и решение')

                goto(t, '/teacher/review/')
                t.get_by_role('link', name='[Демо] Аргумент и два примера', exact=False).first.click(); ready(t)
                mid = int(urlsplit(t.url).path.rstrip('/').split('/')[-1]); facts['saved']['material'] = mid
                t.get_by_text('Записать отзыв к версии 1', exact=True).click()
                form = t.locator('form').filter(has=t.get_by_role('button', name='Сохранить отзыв', exact=True))
                form.locator('select[name=status]').select_option('accepted')
                form.locator('textarea[name=feedback]').fill('Аргумент и примеры согласованы. Работа принята.')
                form.locator('select[name=result]').select_option('4')
                form.get_by_role('button', name='Сохранить отзыв', exact=True).click(); ready(t)
                goto(s, f'/study/materials/{mid}/')
                expect(s.locator('.material-feedback').first).to_contain_text('Результат работы: 4')
                capture(s, 'version-feedback.png')
                fact('Отзыв преподавателя сохранён к точной версии ответа и виден студенту')

            for width, height, filename in ((1366, 900, 'assistant-desktop.png'), (360, 800, 'assistant-mobile.png')):
                s.set_viewport_size({'width': width, 'height': height})
                goto(s, '/study/')
                s.evaluate('window.scrollTo(0, 250)')
                before = s.evaluate('scrollY')
                s.get_by_role('button', name='Помощник', exact=True).click()
                expect(s.locator('.assistant-mode')).to_contain_text('ИИ не подключён', timeout=7000)
                box = s.locator('#page-tool-panel').bounding_box()
                assert box['x'] >= 0 and box['x'] + box['width'] <= width + 1
                assert s.locator('#page-tool-panel').get_attribute('aria-modal') == 'false'
                s.get_by_label('Ваш вопрос', exact=True).fill('Где мои оценки?')
                s.evaluate('window.deanery.validate()'); s.evaluate('window.deanery.validate()')
                expect(s.get_by_label('Ваш вопрос', exact=True)).to_have_value('Где мои оценки?')
                assert abs(s.evaluate('scrollY') - before) <= 2
                expect(s.locator('#privacy-state')).to_be_hidden()
                capture(s, filename)
                s.get_by_role('button', name='Закрыть', exact=True).click()
                fact(f'Помощник {width}px: немодальная панель, сохранены ввод и позиция при проверках сеанса')
            s.set_viewport_size({'width': 360, 'height': 800})
            goto(s, '/today/'); capture(s, 'student-mobile.png')
            s.locator('#reading-toggle').click()
            s.locator('#reading-palette').select_option('dark')
            s.locator('#reading-close').click()
            s.reload(); ready(s)
            expect(s.locator('html')).to_have_attribute('data-reading', 'dark')
            s.locator('.reading-return').click()
            assert s.locator('html').get_attribute('data-reading') is None
            assert s.locator('.bottom-nav a').count() == 5
            fact('На ширине 360 px работают пять разделов, сохранение режима чтения и возврат')
            fact('Снимки текущего интерфейса сделаны в реальном браузере')
        finally:
            (out / 'checks.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding='utf-8')
            browser.close()


if __name__ == '__main__':
    main()
