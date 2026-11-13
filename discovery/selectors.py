"""Scope → matching metadata in PostgreSQL → LIMIT → stable ranking/pages.

No message bodies, files, contacts, private notes or unpublished payloads.
Called inside the established private read snapshot with a final access check.
"""
from django.apps import apps
from django.db import connection
from django.db.models import F, Value, Case, When, TextField, Exists, OuterRef, Q
from django.db.models.functions import Cast, Coalesce, Left, Length, Greatest, LPad
from django.db.models.fields.json import KeyTextTransform
from accounts.selectors import visible_announcements
from data_import.models import ImportRevision, AcademicCoverage
from materials.selectors import assignment_scope
from profiles.models import GroupResource
from profiles.selectors import group_resources, publisher_groups
from student_requests.selectors import request_scope
from student_requests.workflow import REQUEST_TYPES, STATUS_LABELS
from studies.academic_selectors import enrollment_scope
from workspaces.selectors import teaching_scope, notice_scope
from .catalog import SOURCE_LIMIT, PAGE_SIZE, actions, public, ranked, reference_id
from .query import metadata, item, labels, join, date_text


class Candidates:
    def __init__(self, tokens=()):
        self.tokens, self.items, self.sources = tokens, [], []

    def source(self, key, label, state='ready', message='', *, limited=False):
        row = {'key': key, 'label': label, 'state': state, 'message': message, 'limited': limited}
        self.sources.append(row)
        return row

    def take(self, query, key, label):
        # Adapter has already applied access and query-dependent matching.
        rows = list(query[:SOURCE_LIMIT + 1])
        more = len(rows) > SOURCE_LIMIT
        self.source(key, label, 'partial' if more else 'ready',
            f'В этом источнике больше {SOURCE_LIMIT} совпадений. Показаны первые {SOURCE_LIMIT}; уточните запрос.' if more else '',
            limited=more)
        return rows[:SOURCE_LIMIT]


def current_revision(actor, now):
    dataset = (actor.identity.profile.dataset_id if actor.role == 'student' else
               actor.identity.instructor.dataset_id if actor.role == 'teacher' else None)
    return ImportRevision.objects.filter(dataset_id=dataset, revision=F('dataset__current_revision'),
        valid_from__lte=now, valid_until__gt=now).first() if dataset else None


def academic(actor, now, candidates, revision):
    if not revision or revision.schema_version < 4:
        candidates.source('academic', 'Учебные сведения', 'unavailable',
            'Текущие учебные сведения не подтверждены источником. Поиск по ним временно недоступен.')
        return
    coverage = AcademicCoverage.objects.filter(revision=revision, student_id=actor.profile_id,
        period_id=OuterRef('period_id'), completeness='complete')
    scoped = enrollment_scope(actor).alias(
        enrollment_complete=Exists(coverage.filter(entity_type='enrollment')),
        final_complete=Exists(coverage.filter(entity_type='final_result')))
    if not scoped.exists() or scoped.filter(Q(enrollment_complete=False) | Q(final_complete=False)).exists():
        candidates.source('academic_coverage', 'Полнота учебных сведений', 'partial',
            'Учебные сведения подтверждены не полностью. Отсутствие совпадения не означает отсутствия записи.')
    # Только поисковая подпись существующего результата, не классификация/оценка.
    recorded = Case(When(finalresult__result_kind='grade', then=Cast('finalresult__grade', TextField())),
        When(finalresult__pass_status='passed', then=Value('Зачтено')), default=Value('Не зачтено'), output_field=TextField())
    result = Case(When(finalresult__dataset_id=F('dataset_id'), then=recorded),
        When(final_complete=True, then=Value('Итог не внесён')), default=Value('Итог не подтверждён'), output_field=TextField())
    query = metadata(scoped.order_by('course__name', 'pk'), candidates.tokens,
        title=F('course__name'), category=Value('Дисциплина'),
        context=join(F('period__name'), Value(' · '), result), keywords=Value('учеба оценка результат'))
    for row in candidates.take(query, 'academic', 'Дисциплины и результаты'):
        candidates.items.append(item(row, 'course:', f'/study/enrollments/{row["pk"]}/'))


def teaching(actor, now, candidates, revision):
    if not revision:
        candidates.source('teaching', 'Учебные публикации', 'unavailable',
            'Актуальность учебного источника не подтверждена; публикации по дисциплинам исключены из поиска.')
        return
    if actor.role == 'teacher':
        query = metadata(assignment_scope(actor, now).order_by('course__name', 'pk'), candidates.tokens,
            title=F('course__name'), category=Value('Моя дисциплина'),
            context=join(F('group__name'), Value(' · '), F('period__name')))
        for row in candidates.take(query, 'allocations', 'Мои дисциплины'):
            candidates.items.append(item(row, 'allocation:', f'/teacher/courses/{row["pk"]}/'))
    scoped = teaching_scope(actor, now).filter(state='published', published_revision__isnull=False)
    query = metadata(scoped.order_by('-updated_at', '-pk'), candidates.tokens,
        title=Coalesce(KeyTextTransform('title', 'published_revision__payload'), Value('Публикация'), output_field=TextField()),
        category=labels('kind', {'task': 'Задание', 'resource': 'Материал преподавателя'}),
        context=join(F('assignment__course__name'), Value(' · '), F('assignment__period__name')),
        keywords=Left(Coalesce(KeyTextTransform('topic', 'published_revision__payload'), Value(''), output_field=TextField()), 200))
    prefix = '/teacher/publications/' if actor.role == 'teacher' else '/study/teaching/'
    for row in candidates.take(query, 'teaching', 'Материалы и задания'):
        candidates.items.append(item(row, 'teaching:', f'{prefix}{row["pk"]}/'))


def requests(actor, candidates, query):
    scoped, exact = request_scope(actor), reference_id(query)
    if exact is not None:
        scoped = scoped.filter(pk=exact)
    number = Cast('pk', TextField())
    query = metadata(scoped.order_by('-created_at', '-pk'), () if exact is not None else candidates.tokens,
        title=join(Value('З-'), LPad(number, Greatest(Value(6), Length(number)), Value('0')),
                   Value(' · '), labels('request_type', REQUEST_TYPES)), category=Value('Заявка'),
        context=join(labels('status', STATUS_LABELS), Value(' · '), date_text('updated_at')))
    prefix = '/staff/requests/' if actor.role == 'staff' else '/requests/'
    for row in candidates.take(query, 'requests', 'Заявки'):
        candidates.items.append(item(row, 'request:', f'{prefix}{row["pk"]}/'))


def notices(actor, now, candidates):
    scoped = notice_scope(actor, now).filter(state='published', published_revision__isnull=False)
    query = metadata(scoped.order_by('-updated_at', '-pk'), candidates.tokens,
        title=Coalesce(KeyTextTransform('title', 'published_revision__payload'), Value('Объявление'), output_field=TextField()),
        category=Value('Объявление службы'), context=Value('Доступная публикация факультета'))
    prefix = '/staff/notices/' if actor.role == 'staff' else '/notices/'
    for row in candidates.take(query, 'faculty_notices', 'Объявления службы'):
        candidates.items.append(item(row, 'notice:', f'{prefix}{row["pk"]}/'))
    if actor.role == 'student':
        query = metadata(visible_announcements(actor, now=now).order_by('-published_at', '-pk'), candidates.tokens,
            title=F('title'), category=Value('Объявление'), context=join(Value('Опубликовано '), date_text('published_at')))
        for row in candidates.take(query, 'announcements', 'Учебные объявления'):
            candidates.items.append(item(row, 'announcement:', f'/announcements/{row["pk"]}/'))


def resources(actor, now, candidates):
    scoped = group_resources(actor, now) if actor.role == 'student' else GroupResource.objects.filter(
        group__in=publisher_groups(actor), archived=False, available_from__lte=now, available_until__gt=now)
    query = metadata(scoped.order_by('title', 'pk'), candidates.tokens, title=F('title'),
        category=Value('Ресурс группы'), context=labels('category', dict(GroupResource._meta.get_field('category').choices)))
    for row in candidates.take(query, 'resources', 'Ресурсы группы'):
        url = '/profile/group/#group-resources' if actor.role == 'student' else f'/staff/resources/{row["pk"]}/'
        candidates.items.append(item(row, 'resource:', url))


def journeys(actor, candidates):
    if not apps.is_installed('journeys'):
        candidates.source('journeys', 'Мои ситуации', 'unavailable', 'Подготовка ситуаций временно недоступна.')
        return
    from journeys.selectors import journey_scope
    titles = {'exchange': 'Подготовиться к учебному обмену', 'correction': 'Проверить неточность в учебных сведениях'}
    query = metadata(journey_scope(actor).order_by('-pk'), candidates.tokens, title=labels('template_key', titles),
        category=Value('Моя ситуация'), context=join(Value('Сохранённая подготовка · редакция '), Cast('version', TextField())))
    for row in candidates.take(query, 'journeys', 'Мои ситуации'):
        candidates.items.append(item(row, 'journey:', f'/journeys/{row["pk"]}/'))


def consultations(actor, now, candidates):
    if not apps.is_installed('consultations'):
        candidates.source('consultations', 'Консультации', 'unavailable', 'Запись на консультации временно недоступна.')
        return
    from consultations.selectors import discovery_records
    discovery_records(actor, now, candidates)


def search(actor, now, query, tokens, page=1):
    suggestions = actions(actor.role)
    if not query:
        return {'marker': actor.marker, 'state': 'ready', 'items': [], 'count': 0, 'page': 1, 'pages': 1,
            'page_size': PAGE_SIZE, 'partial': False, 'limited': False, 'sources': [], 'suggestions': [public(item) for item in suggestions]}
    # The read snapshot owns this LOCAL setting; a cancelled query rolls it back.
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout = '2000ms'")
    candidates = Candidates(tokens)
    candidates.items.extend(suggestions)
    exact = reference_id(query)
    if exact is not None:
        if actor.role in ('student', 'staff'):
            requests(actor, candidates, query)
    else:
        revision = current_revision(actor, now)
        if actor.role == 'student':
            academic(actor, now, candidates, revision)
            teaching(actor, now, candidates, revision)
            requests(actor, candidates, query)
            notices(actor, now, candidates)
            resources(actor, now, candidates)
            journeys(actor, candidates)
        elif actor.role == 'teacher':
            teaching(actor, now, candidates, revision)
        else:
            requests(actor, candidates, query)
            notices(actor, now, candidates)
            resources(actor, now, candidates)
        consultations(actor, now, candidates)
    matches = ([row for row in candidates.items if row['key'] == 'request:' + str(exact)]
               if exact is not None else ranked(candidates.items, query, tokens))
    count = len(matches)
    pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    selected = min(page, pages)
    return {'marker': actor.marker, 'state': 'ready', 'items': [public(row) for row in matches[(selected - 1) * PAGE_SIZE:selected * PAGE_SIZE]],
        'count': count, 'page': selected, 'pages': pages, 'page_size': PAGE_SIZE,
        'partial': any(row['state'] != 'ready' for row in candidates.sources),
        'limited': any(row['limited'] for row in candidates.sources), 'sources': candidates.sources,
        'suggestions': [public(row) for row in suggestions]}
