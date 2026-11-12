"""Декларативные поля для React; права и значения готовятся на сервере."""
from datetime import datetime,timedelta
from django.utils import timezone
from studies.selectors import moscow
from materials.selectors import assignment_choices
from materials.uploads import KINDS
from profiles.selectors import publisher_groups
from .selectors import ordered_files,file_values

def field(name,label,value='',type='text',**extra):
    return {'name':name,'label':label,'value':value,'type':type,**extra}

def local(value):
    if not value:return ''
    return moscow(datetime.fromisoformat(value) if isinstance(value,str) else value).strftime('%Y-%m-%dT%H:%M')

def teaching_editor(actor,now,row=None,kind='resource',assignment=None):
    if row: kind=row.kind
    p=row.draft if row else {}
    choices=assignment_choices(actor,now)
    fields=[field('assignment','Дисциплина, группа и период',str(row.assignment_id if row else assignment or (choices[0]['id'] if choices else '')),'select',
        options=[[str(a['id']),f"{a['course']} · {a['group']} · {a['period']}"] for a in choices],locked=bool(row)),
        field('kind','Вид публикации',kind,'hidden'),field('title','Название',p.get('title',''),maxLength=160,required=True),
        field('topic','Тема или краткое описание',p.get('topic',''),maxLength=500),
        field('body','Инструкции задания' if kind=='task' else 'Текст материала',p.get('body',''),'textarea',maxLength=20000),
        field('links','HTTPS-ссылки · каждая с новой строки','\n'.join(p.get('links',[])),'textarea',maxLength=5000)]
    if kind=='task':
        fields.extend([field('opens_at','Приём открывается · МСК',local(p.get('opens_at')),'datetime-local',group='Условия задания'),
            field('due_at','Срок сдачи · МСК',local(p.get('due_at')),'datetime-local',group='Условия задания'),
            field('cutoff_at','Явное окончание приёма · необязательно',local(p.get('cutoff_at')),'datetime-local',group='Условия задания'),
            field('modes','Форматы ответа',p.get('modes',['text']),'multi',options=[['text','Текст'],['link','Ссылка на облако'],['email','Контактная почта']],group='Форматы и файлы'),
            field('text_limit','Лимит текста ответа',p.get('text_limit',5000),'number',min=100,max=20000,group='Форматы и файлы'),
            field('attachment_types','Типы вложений ответа',p.get('attachment_types',list(KINDS)),'multi',options=[[k,k.upper()] for k in KINDS],group='Форматы и файлы'),
            field('attachment_count','Максимум файлов',p.get('attachment_count',5),'number',min=1,max=5,group='Форматы и файлы'),
            field('attachment_file_mib','МиБ на файл',p.get('attachment_file_limit',10*1048576)//1048576,'number',min=1,max=10,group='Форматы и файлы'),
            field('attachment_total_mib','МиБ вместе',p.get('attachment_total_limit',25*1048576)//1048576,'number',min=1,max=25,group='Форматы и файлы'),
            field('assessment_mode','Результат работы',p.get('assessment_mode','none'),'select',options=[['none','Без отметки'],['numeric','Отметка 2–5'],['pass_fail','Зачёт / незачёт']],group='Условия задания')])
    actions=[['save','Сохранить черновик'],['publish','Опубликовать']]
    if row and row.published_revision_id:
        if row.state=='published': actions+=[['withdraw','Отозвать'],['archive','В архив']]
        if kind=='task' and row.state=='published':
            actions+=[['close','Закрыть приём']] if row.published_revision.payload['accepting'] else [['reopen','Открыть приём']]
            actions+=[['add_members','Добавить текущих участников']]
    return {'marker':actor.marker,'title':'Задание' if kind=='task' else 'Учебный материал','fields':fields,'actions':actions,
        'version':row.version if row else 0,'id':row.pk if row else None,'state_label':row.get_kind_display()+' · '+row.state if row else 'Новая публикация',
        'endpoint':f'/teacher/publications/{row.pk if row else "new"}/editor/','url':f'/teacher/publications/{row.pk}/' if row else '',
        'files':[file_values(f) for f in ordered_files(row.pk,row.draft_files)] if row else [],'uploads':True,
        'hint':'Публикация отправит текущий текст и выбранные файлы. Сохранение черновика не меняет доступную студентам версию.',
        'task':kind=='task'}


def notice_editor(actor,row=None):
    p=row.draft if row else {}
    fields=[field('group','Аудитория',str(row.group_id or '') if row else '','select',locked=bool(row),
        options=[['','Весь обслуживаемый факультет']]+[[str(g.pk),g.name] for g in publisher_groups(actor)]),
        field('title','Заголовок',p.get('title',''),maxLength=180,required=True),field('body','Текст объявления',p.get('body',''),'textarea',maxLength=4000),
        field('link','HTTPS-ссылка · необязательно',p.get('link',''),'url',maxLength=1000),
        field('visible_until','Показывать до · МСК',local(p.get('visible_until')),'datetime-local'),
        field('due_at','Информационный срок · МСК',local(p.get('due_at')),'datetime-local')]
    actions=[['save','Сохранить черновик'],['publish','Опубликовать сейчас']]
    if row and row.published_revision_id and row.state=='published':actions.append(['withdraw','Отозвать'])
    return {'marker':actor.marker,'title':'Объявление службы','fields':fields,'actions':actions,'version':row.version if row else 0,
        'id':row.pk if row else None,'endpoint':f'/staff/notices/{row.pk if row else "new"}/editor/',
        'url':f'/staff/notices/{row.pk}/' if row else '', 'uploads':False,'files':[],
        'hint':'Публикация выполняется сразу. Срок объявления не становится учебной задолженностью.'}


def resource_editor(actor,row=None):
    fields=[field('group','Группа',str(row.group_id) if row else '','select',locked=bool(row),options=[[str(g.pk),g.name] for g in publisher_groups(actor)]),
        field('category','Раздел',row.category if row else 'document','select',options=[['document','Документы'],['course','Курсы'],['internship','Практика']]),
        field('title','Заголовок',row.title if row else '',maxLength=160,required=True),
        field('description','Описание',row.description if row else '','textarea',maxLength=2000),
        field('url','HTTPS-ссылка · необязательно',row.url if row else '','url',maxLength=500),
        field('available_from','Показывать с · МСК',local(row.available_from if row else timezone.now()),'datetime-local'),
        field('available_until','Показывать до · МСК',local(row.available_until if row else timezone.now()+timedelta(days=30)),'datetime-local')]
    return {'marker':actor.marker,'title':'Ресурс группы','fields':fields,'actions':[['save','Сохранить и показать группе']]+([['restore','Вернуть из архива']] if row and row.archived else [['archive','В архив']] if row else []),
        'version':row.version if row else 0,'id':row.pk if row else None,'endpoint':f'/staff/resources/{row.pk if row else "new"}/editor/',
        'url':f'/staff/resources/{row.pk}/' if row else '', 'uploads':False,'files':[],
        'hint':'Сохранение создаёт версию ресурса. Группа видит его в указанный период, если он не архивирован.'}
