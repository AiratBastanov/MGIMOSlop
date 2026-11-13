"""Совместимый с каталогом отбор метаданных PostgreSQL до LIMIT.

Никаких текстов работ/сообщений: адаптер передаёт только разрешённые поля.
SQL использует те же слова, синонимы и префикс длиной от трёх символов.
"""
import re
from django.db.models import Case, When, Value, TextField, F, Func
from django.db.models.functions import Cast, Coalesce, Concat, Left, Replace, Collate
from .catalog import SYNONYMS


def labels(field, choices):
    return Case(*(When(**{field: key}, then=Value(label)) for key, label in choices.items()),
                default=Value(''), output_field=TextField())


def join(*parts):
    return Concat(*parts, output_field=TextField())


def date_text(field, *, time=False):
    local = Func(Value('Europe/Moscow'), F(field), function='timezone')
    return Func(local, Value('DD.MM.YYYY HH24:MI' if time else 'DD.MM.YYYY'),
                function='to_char', output_field=TextField())


def normalized(expression):
    expression = Func(expression, function='normalize', template='normalize(%(expressions)s, NFKC)', output_field=TextField())
    # Независимо от lc_ctype БД: алфавит Python words(), Ё/ё и casefold,
    # включая символы, чей fold создаёт латинские буквы с combining marks.
    expression = Func(expression, Value('ABCDEFGHIJKLMNOPQRSTUVWXYZАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯёᲀᲁᲂᲃᲄᲅᲆ'),
        Value('abcdefghijklmnopqrstuvwxyzабвгдеежзийклмнопрстуфхцчшщъыьэюяевдосттъ'), function='translate', output_field=TextField())
    for before, after in (('ß', 'ss'), ('ẞ', 'ss'), ('İ', 'i\u0307'), ('ǰ', 'j\u030c'),
                          ('ẖ', 'h\u0331'), ('ẗ', 't\u0308'), ('ẘ', 'w\u030a'), ('ẙ', 'y\u030a'), ('ẚ', 'aʾ')):
        expression = Replace(expression, Value(before), Value(after), output_field=TextField())
    return expression


def metadata(query, tokens, *, title, category, context, keywords=Value('')):
    query = query.annotate(
        search_title=Left(Coalesce(Cast(title, TextField()), Value(''), output_field=TextField()), 200),
        search_category=category,
        search_context=Left(Coalesce(Cast(context, TextField()), Value(''), output_field=TextField()), 280),
        search_keywords=Coalesce(Cast(keywords, TextField()), Value(''), output_field=TextField()))
    query = query.alias(search_text=Collate(normalized(join(F('search_title'), Value(' '), F('search_category'),
        Value(' '), F('search_context'), Value(' '), F('search_keywords'))), 'C'))
    for token in tokens:
        variants = next((group for group in SYNONYMS if token in group), frozenset((token,)))
        exact = '|'.join(re.escape(word) for word in sorted(variants))
        endings = [f'(?:{exact})(?:$|[^a-zа-я0-9])']
        if len(token) >= 3:
            endings.append(re.escape(token) + '[a-zа-я0-9]*')
        query = query.filter(search_text__regex='(?:^|[^a-zа-я0-9])(?:' + '|'.join(endings) + ')')
    return query.values('pk', 'search_title', 'search_category', 'search_context', 'search_keywords')


def item(row, key, url):
    from .catalog import entry
    return entry(key + str(row['pk']), row['search_title'], row['search_category'], row['search_context'],
                 url, keywords=row['search_keywords'])
