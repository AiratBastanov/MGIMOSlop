"""Запись реального локального демо Playwright. Сырые видео и сессии приватны.

Перед запуском подготовьте отдельную синтетическую БД штатным prepare_demo.
Никаких подмен ответов, DOM, времени или официальных учебных фактов.
"""
import argparse,json,os,re,time
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright,expect

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--base',required=True)
parser.add_argument('--credentials',type=Path,required=True)
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--synthetic-database',required=True)
parser.add_argument('--resume',action='store_true')
args=parser.parse_args()
assert urlsplit(args.base).hostname in ('127.0.0.1','localhost')
assert args.synthetic_database.startswith('deanery_film_')
args.output.mkdir(parents=True,exist_ok=args.resume)
credentials=json.loads(args.credentials.read_text(encoding='utf-8'))
manifest_path=args.output/'capture.json'
manifest=json.loads(manifest_path.read_text(encoding='utf-8')) if args.resume else {'database':args.synthetic_database,'base':args.base,'scenes':[],'receipts':{},'audio':False}
def save():manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
def ready(p):
    p.wait_for_function('() => window.deanery?.active===true');p.evaluate('document.fonts.ready')
def go(p,path):
    response=p.goto(args.base+path);assert response.status==200,(path,response.status);ready(p)
def login(p,user):
    p.goto(args.base+'/login/');p.locator('[name=username]').fill(user);p.locator('[name=password]').fill(credentials[user]);p.locator('button[type=submit]').click();ready(p)
def link(p,path):p.locator('.desktop-nav a[href="'+path+'"]').click();ready(p)
def type_text(locator,value):locator.click();locator.press_sequentially(value,delay=45)
def confirmed(p,name):
    expect(p.locator('.inbox-component .view-switch [aria-pressed=true]')).to_have_text(name,timeout=20000)
    expect(p.locator('.inbox-entries')).to_have_attribute('aria-busy','false',timeout=20000)

with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='chrome',headless=True)
    states={}
    # Authentication never belongs to a recording context.
    for user in ('student_a','teacher_demo','staff_demo'):
        c=browser.new_context(locale='ru-RU',timezone_id='Europe/Moscow');p=c.new_page();login(p,user)
        states[user]=c.storage_state();c.close()
    probe=browser.new_context(storage_state=states['student_a']);p=probe.new_page()
    response=p.goto(args.base+'/health/ready/');manifest['build']=response.headers['x-deanery-build'];probe.close();save()
    def scene(number,user,path,target,perform,mobile=False):
        if any(s['number']==number for s in manifest['scenes']):return
        viewport={'width':360,'height':800} if mobile else {'width':1920,'height':984}
        context=browser.new_context(storage_state=states[user],viewport=viewport,record_video_size=viewport,record_video_dir=str(args.output/'raw'),locale='ru-RU',timezone_id='Europe/Moscow',device_scale_factor=1)
        page=context.new_page();video=page.video
        try:
            go(page,path)
            if page.locator('.inbox-component').count():confirmed(page,'Входящие')
            page.wait_for_timeout(1000)
            started=time.monotonic();cues=[]
            def caption(text):cues.append({'at':round(time.monotonic()-started,3),'text':text})
            perform(page,caption)
            remaining=target-(time.monotonic()-started)
            if remaining>0:page.wait_for_timeout(remaining*1000)
            duration=round(time.monotonic()-started,3)
            page.screenshot(path=str(args.output/f'scene-{number:02d}-end.png'))
            context.close()
            manifest['scenes'].append({'number':number,'role':user,'viewport':viewport,'mobile':mobile,'raw':str(video.path().resolve()),'duration':duration,'cues':cues})
            save();print(f'Сцена {number}: {duration:.1f} с; подтверждена.',flush=True)
        except BaseException:
            page.screenshot(path=str(args.output/f'scene-{number:02d}-failure.png'))
            context.close();save();raise

    def student_intro(p,cap):
        cap('Цифровой деканат · конкурсный прототип. Все сведения вымышлены.')
        p.wait_for_timeout(3500);p.locator('.profile-trigger').click();p.wait_for_timeout(3500)
        cap('Профиль открывается над кнопкой. Здесь учебная группа, контакты и настройки.')
        p.locator('#profile-panel a[href="/profile/"]').click();ready(p);p.wait_for_timeout(5000)
        link(p,'/today/');cap('«Сегодня» объединяет учебный день, ближайшие события и важные вопросы.');p.wait_for_timeout(6500)
        p.locator('.header-search').click();ready(p);type_text(p.locator('#cabinet-search'),'расписание')
        p.get_by_role('button',name='Найти',exact=True).click();p.locator('#search-results a[href="/schedule/"]').wait_for()
        cap('Обычный запрос «расписание» находит разрешённый раздел кабинета.');p.wait_for_timeout(3000)
        p.locator('#search-results a[href="/schedule/"]').click();ready(p);p.wait_for_timeout(2200)
        p.get_by_role('button',name='Календарь',exact=True).click();p.wait_for_timeout(1800)
        p.locator('dialog').get_by_role('button',name=re.compile('23 ноября 2026')).click()
        expect(p.locator('.date-period')).to_contain_text('23');p.wait_for_timeout(2500)
        p.get_by_role('button',name='Неделя',exact=True).click();expect(p.locator('.view-switch [aria-pressed=true]')).to_have_text('Неделя')
        cap('Выбираем неделю 23 ноября: занятия, время, аудитории и преподаватели.');p.wait_for_timeout(5000)
        p.locator('.header-search').click();ready(p);type_text(p.locator('#cabinet-search'),'оценки');p.get_by_role('button',name='Найти',exact=True).click()
        p.get_by_role('link',name='Найти все оценки',exact=True).click();ready(p)
        cap('Все внесённые результаты за доступные периоды. Отсутствие итога не становится оценкой.')
        assert 'filter=assessed' in p.url
    scene(1,'student_a','/profile/',62,student_intro)

    def request(p,cap):
        cap('Студент отправляет настоящую заявку на демонстрационную справку.');p.wait_for_timeout(3000)
        p.locator('#request-purpose').select_option('scholarship');type_text(p.locator('#request-recipient'),'Демонстрационный стипендиальный фонд')
        p.get_by_role('button',name='Увеличить количество').click();type_text(p.locator('#request-comment'),'Для участия в учебной стипендиальной программе.')
        p.wait_for_timeout(3500);p.get_by_role('button',name='Отправить заявку',exact=True).click();p.wait_for_url(re.compile(r'/requests/\d+/$'));ready(p)
        expect(p.locator('.request-current-status')).to_have_text('Принята')
        identifier=int(p.url.rstrip('/').split('/')[-1]);manifest['receipts']['request_id']=identifier;save()
        cap('Заявка сохранена: появился номер и статус «Принята».');p.wait_for_timeout(6000)
    scene(2,'student_a','/requests/new/?type=study_certificate',30,request)

    def staff(p,cap):
        cap('Служба факультета видит ту же заявку и данные, сохранённые при подаче.');p.wait_for_timeout(4000)
        p.get_by_role('button',name='Взять в работу',exact=True).click();ready(p);expect(p.locator('.request-current-status')).to_have_text('В работе');p.wait_for_timeout(3000)
        form=p.locator('form.decision-form').filter(has=p.locator('input[name=action][value=complete]'))
        type_text(form.locator('textarea'),'Справка подготовлена. Получение в демонстрационной приёмной, кабинет 102.')
        cap('Сотрудник записывает ответ и завершает обработку через обычную форму.');p.wait_for_timeout(2500)
        form.get_by_role('button',name='Завершить обработку',exact=True).click();ready(p);expect(p.locator('.request-current-status')).to_have_text('Готова')
        p.locator('#request-current').scroll_into_view_if_needed();p.wait_for_timeout(6000)
        manifest['receipts']['request_status']='ready';save()
    scene(3,'staff_demo',f"/staff/requests/{manifest['receipts']['request_id']}/",32,staff)

    def response_notifications(p,cap):
        cap('Студент возвращается к заявке: статус «Готова» и ответ службы уже сохранены.')
        expect(p.locator('.request-current-status')).to_have_text('Готова');p.wait_for_timeout(4500)
        p.reload();ready(p);expect(p.locator('#request-current')).to_contain_text('кабинет 102');p.wait_for_timeout(3500)
        link(p,'/notifications/');confirmed(p,'Входящие');cap('Уведомления без пустой полосы: события и текущие статусы сразу видны.');p.wait_for_timeout(3500)
        cap('При переключении показан настоящий ход загрузки; старые строки сохраняются до ответа.')
        p.get_by_role('button',name='История',exact=True).click();confirmed(p,'История');p.wait_for_timeout(4500)
        p.get_by_role('button',name='Входящие',exact=True).click();confirmed(p,'Входящие');p.wait_for_timeout(4000)
    scene(4,'student_a',f"/requests/{manifest['receipts']['request_id']}/",30,response_notifications)

    def review(p,cap):
        cap('Преподаватель открывает уже отправленную работу Алины — версию 1.');p.wait_for_timeout(3500)
        p.get_by_role('link',name='[Демо] Аргумент и два примера',exact=True).click();ready(p);p.wait_for_timeout(4500)
        p.get_by_text('Записать отзыв к версии 1',exact=True).click()
        form=p.locator('form').filter(has=p.locator('textarea[name=feedback]'))
        form.locator('select[name=status]').select_option('accepted')
        type_text(form.locator('textarea[name=feedback]'),'Аргумент ясен, оба примера связаны с выводом. Версия 1 принята.')
        form.locator('select[name=result]').select_option('5');p.wait_for_timeout(3000)
        cap('Отзыв и результат относятся к этой версии работы. Официальная оценка не меняется.')
        form.get_by_role('button',name='Сохранить отзыв').click();ready(p)
        expect(p.locator('.material-feedback')).to_contain_text('Версия 1 принята.');p.locator('.material-feedback').scroll_into_view_if_needed();p.wait_for_timeout(6000)
        manifest['receipts']['material_review']={'material':1,'version':1,'status':'accepted','result':'5'};save()
    scene(5,'teacher_demo','/teacher/review/',35,review)

    def student_review(p,cap):
        cap('Вернувшись к своим материалам, студент видит сохранённый отзыв преподавателя.');p.wait_for_timeout(3000)
        p.get_by_role('link',name='[Демо] Аргумент и два примера',exact=True).click();ready(p)
        expect(p.locator('.material-feedback')).to_contain_text('Версия 1 принята.');p.locator('.material-feedback').scroll_into_view_if_needed();p.wait_for_timeout(6500)
        p.reload();ready(p);expect(p.locator('.material-feedback')).to_contain_text('Версия 1 принята.')
        cap('После обновления страницы версия, содержание и отзыв остаются вместе.');p.wait_for_timeout(6000)
    scene(6,'student_a','/study/materials/',24,student_review)

    def preparation(p,cap):
        cap('Личная подготовка помогает собрать вопросы к учебному обмену.');p.wait_for_timeout(2500)
        p.get_by_role('link',name='Продолжить подготовку',exact=True).click();ready(p);p.locator('#journey-consultations').wait_for(state='attached',timeout=10000) if p.locator('#journey-consultations').count() else None
        p.wait_for_timeout(5000);go(p,'/consultations/')
        cap('В консультациях видны подтверждённая запись и свободное время своей аудитории.');p.wait_for_timeout(5000)
    scene(7,'student_a','/journeys/',18,preparation)

    def mobile(p,cap):
        cap('Мобильный браузер · 360 CSS px. Это эмуляция окна, не съёмка физического телефона.');p.wait_for_timeout(4500)
        p.locator('.bottom-nav a[href="/requests/"]').click();ready(p);p.wait_for_timeout(5000)
        p.mouse.wheel(0,-2000);p.wait_for_timeout(1000);p.locator('#reading-toggle').click()
        cap('Режим для слабовидящих включается одним действием. Размер и палитра настраиваются.');p.wait_for_timeout(4500)
        p.locator('#reading-palette').select_option('dark');p.wait_for_timeout(2500);p.locator('#reading-close').click();p.wait_for_timeout(4000)
        p.locator('.reading-return').click();cap('«Обычный вид» возвращает оформление. Сохранённые заявки и ответы остаются.');p.wait_for_timeout(4500)
    scene(8,'student_a','/today/',31,mobile,mobile=True)
    browser.close();save()
print('Сырые записи закрыты. До готового видео нужны монтаж и просмотр.',flush=True)
