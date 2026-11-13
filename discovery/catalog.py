"""Короткий проверенный каталог действий; запрос никогда не выполняет действие."""
import re
import unicodedata

QUERY_LIMIT = 100
TOKEN_LIMIT = 8
PAGE_SIZE = 10
SOURCE_LIMIT = 200

# Deliberately explicit, reviewed service language, not inferred eligibility.
SYNONYMS = (
    frozenset(('справка', 'справку', 'справки', 'справок', 'certificate')),
    frozenset(('обмен', 'обмена', 'обмену', 'exchange', 'learning', 'agreement', 'cv', 'мотивация', 'транскрипт')),
    frozenset(('консультация', 'консультации', 'консультацию', 'консультаций', 'записаться', 'прием', 'приема', 'встреча', 'appointment')),
    frozenset(('оценки', 'оценка', 'оценку', 'результаты', 'результат', 'успеваемость', 'зачеты', 'grades')),
    frozenset(('исправление', 'исправить', 'неточность', 'неточности', 'ошибка', 'ошибку', 'коррекция')),
    frozenset(('задание', 'задания', 'заданий', 'домашнее', 'работа', 'task')),
)
STOP_WORDS = frozenset(('как', 'где', 'мне', 'мой', 'мои', 'на', 'в', 'во', 'к', 'ко', 'по', 'об', 'о', 'для', 'и'))


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().replace('ё', 'е').split())


def words(value):
    return re.findall(r'[a-zа-я0-9]+', normalize(value))


def parse_query(value, page='1'):
    if not isinstance(value, str) or len(value) > QUERY_LIMIT or any(ord(c) < 32 and c not in '\t\n\r' for c in value):
        raise ValueError('Введите запрос длиной до 100 символов.')
    query = normalize(value)
    tokens = tuple(dict.fromkeys(word for word in words(query) if word not in STOP_WORDS))
    if len(tokens) > TOKEN_LIMIT:
        raise ValueError('Оставьте до восьми ключевых слов: название, номер заявки или действие.')
    if query and not tokens:
        raise ValueError('Введите название, номер заявки или нужное действие.')
    if isinstance(page, bool) or not str(page).isdigit() or not 1 <= int(page) <= 100:
        raise ValueError('Откройте поиск заново: номер страницы недопустим.')
    return query, tokens, int(page)


def reference_id(query):
    match = re.fullmatch(r'(?:з\s*[-–—]?\s*)?([0-9]{1,12})', query)
    return int(match.group(1)) if match else None


def entry(key, title, category, context, url, *, kind='object', keywords=''):
    return {'key': str(key), 'kind': kind, 'title': str(title)[:200], 'category': category,
            'context': str(context)[:280], 'url': url, '_keywords': keywords}


def actions(role):
    if role == 'student':
        rows = (
            ('certificate', 'Заказать справку об обучении', 'Открыть форму заявки', '/requests/new/?type=study_certificate', 'справка документ'),
            ('exchange', 'Подготовиться к учебному обмену', 'План, вопросы и проверка подготовки', '/journeys/?template=exchange', 'обмен learning agreement cv мотивация'),
            ('consultation', 'Записаться на консультацию', 'Выбрать доступное время преподавателя или службы', '/consultations/', 'консультация прием запись встреча'),
            ('grades', 'Найти все оценки', 'Дисциплины и внесённые результаты', '/study/?period=all', 'оценки успеваемость зачеты результаты'),
            ('tasks', 'Открыть учебные задания', 'Условия и отправка работы преподавателю', '/study/teaching/?kind=task', 'задания домашнее работа материалы'),
            ('correction', 'Проверить неточность в учебных сведениях', 'Выбрать сведения и подать существующий тип заявки', '/journeys/?template=correction', 'ошибка исправить неточность'),
        )
    elif role == 'teacher':
        rows = (
            ('courses', 'Открыть мои дисциплины', 'Текущие назначения, публикации и задания', '/teacher/courses/', 'курс дисциплина задания материал'),
            ('review', 'Проверить отправленные работы', 'Очередь работ в ваших назначениях', '/teacher/review/', 'проверка работа отзыв'),
            ('consultation', 'Организовать консультацию', 'Время приёма для своих учебных групп', '/consultations/', 'консультация запись прием встреча'),
        )
    else:
        rows = (
            ('requests', 'Открыть заявки факультета', 'Очередь, уточнения и сохранённые решения', '/staff/requests/', 'справка заявление обмен план заявки'),
            ('consultation', 'Организовать приём студентов', 'Время консультации для своего факультета', '/consultations/', 'консультация запись прием встреча'),
            ('notices', 'Открыть объявления службы', 'Публикации в пределах своего факультета', '/staff/notices/', 'объявление новости'),
        )
    return [entry('action:' + key, title, 'Действие', context, url, kind='action', keywords=keywords)
            for key, title, context, url, keywords in rows]


def token_score(token, title_words, metadata_words):
    if token in title_words:
        return 8
    if token in metadata_words:
        return 5
    variants = next((group for group in SYNONYMS if token in group), frozenset((token,)))
    if variants.intersection(title_words):
        return 6
    if variants.intersection(metadata_words):
        return 3
    if len(token) >= 3 and any(word.startswith(token) for word in title_words):
        return 4
    if len(token) >= 3 and any(word.startswith(token) for word in metadata_words):
        return 2
    return 0


def ranked(items, query, tokens):
    ranked_items = []
    for item in items:
        title_words = frozenset(words(item['title']))
        metadata_words = frozenset(words(' '.join((item['category'], item['context'], item.get('_keywords', '')))))
        scores = [token_score(token, title_words, metadata_words) for token in tokens]
        if not all(scores):
            continue
        score = sum(scores) + (20 if normalize(item['title']) == query else 0)
        # Matching verified actions stay reachable even when many personal
        # records repeat the same service word. Exact references are handled
        # separately by the scoped adapter and still return only that request.
        ranked_items.append((0 if item['kind'] == 'action' else 1,
                             -score, normalize(item['title']), item['key'], item))
    ranked_items.sort(key=lambda row: row[:4])
    return [row[4] for row in ranked_items]


def public(item):
    return {key: value for key, value in item.items() if not key.startswith('_')}
