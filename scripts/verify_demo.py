"""Browser smoke for a disposable local demonstration. Credentials stay local."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--credentials', default='.local/demo-credentials.json')
    parser.add_argument('--output', required=True)
    parser.add_argument('--read-only', action='store_true', help='Only login/read/helper; no new request or review.')
    parser.add_argument('--provider-state', choices=('disabled', 'unavailable'), default='disabled')
    args = parser.parse_args()
    if urlsplit(args.base_url).hostname not in ('127.0.0.1', 'localhost'):
        parser.error('Проверка предназначена только для локального демо.')
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    credentials = json.loads(Path(args.credentials).read_text(encoding='utf-8'))
    facts = {'checks': [], 'saved': {}, 'screenshots': []}

    def fact(name):
        facts['checks'].append(name)
        (out / 'checks.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding='utf-8')
        print(name, flush=True)

    def ready(page):
        expect(page.locator('#privacy-state')).to_be_hidden()
        page.wait_for_function('window.deanery?.active === true')
        for el in page.locator('[data-component]').all():
            expect(el).not_to_be_empty()
        page.evaluate('document.fonts.ready')

    def goto(page, path):
        response = page.goto(args.base_url + path)
        assert response.status == 200, (path, response.status)
        ready(page)

    def login(page, user):
        page.goto(args.base_url + '/login/')
        page.get_by_label('Логин', exact=True).fill(user)
        page.get_by_label('Пароль', exact=True).fill(credentials[user])
        page.get_by_role('button', name='Войти', exact=False).click()
        ready(page)

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
            for link in files.all():
                url = args.base_url + link.get_attribute('href')
                assert contexts['student_a'].request.get(url).status == 200
                assert contexts['student_b'].request.get(url).status == 404
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
            fact('Снимки текущего интерфейса сделаны в реальном браузере')
        finally:
            (out / 'checks.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding='utf-8')
            browser.close()


if __name__ == '__main__':
    main()
