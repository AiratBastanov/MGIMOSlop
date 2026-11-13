"""Source adapters: scope first, bounded metadata second, matching/counts last.

Never read message bodies, attachments, contacts, private notes or student drafts.
Call inside the established private read snapshot; its owner rechecks authority.
"""
from django.apps import apps
from django.db.models import F
from accounts.selectors import visible_announcements
from data_import.models import ImportRevision, AcademicCoverage
from materials.selectors import assignment_scope
from profiles.models import GroupResource
from profiles.selectors import group_resources, publisher_groups
from student_requests.selectors import request_scope
from student_requests.workflow import REQUEST_TYPES, STATUS_LABELS
from studies.academic_selectors import enrollment_scope, result_values
from studies.selectors import moscow
from workspaces.selectors import teaching_scope, notice_scope
from .catalog import (SOURCE_LIMIT, PAGE_SIZE, actions, entry, public, ranked, reference_id)


class Candidates:
    def __init__(self):
        self.items = []
        self.sources = []

    def source(self, key, label, state='ready', message=''):
        row = {'key': key, 'label': label, 'state': state, 'message': message}
        self.sources.append(row)
        return row

    def take(self, query, key, label):
        # Query must already be scoped; the cap cannot reveal a foreign object.
        rows = list(query[:SOURCE_LIMIT + 1])
        more = len(rows) > SOURCE_LIMIT
        self.source(key, label, 'partial' if more else 'ready',
                    'Показана ограниченная выборка доступных записей. Уточните запрос или откройте раздел.' if more else '')
        return rows[:SOURCE_LIMIT]


def current_revision(actor, now):
    if actor.role == 'student':
        dataset = actor.identity.profile.dataset_id
    elif actor.role == 'teacher':
        dataset = actor.identity.instructor.dataset_id
    else:
        return None
    return ImportRevision.objects.filter(dataset_id=dataset, revision=F('dataset__current_revision'),
        valid_from__lte=now, valid_until__gt=now).first()


def academic(actor, now, candidates, revision):
    if not revision or revision.schema_version < 4:
        candidates.source('academic', 'Учебные сведения', 'unavailable',
            'Текущие учебные сведения не подтверждены источником. Поиск по ним временно недоступен.')
        return
    rows = candidates.take(enrollment_scope(actor).select_related('course', 'period', 'finalresult')
        .order_by('course__name', 'pk'), 'academic', 'Дисциплины и результаты')
    coverage = list(AcademicCoverage.objects.filter(revision=revision, student_id=actor.profile_id,
        period_id__in={row.period_id for row in rows}, entity_type__in=('enrollment', 'final_result'))
        .values('period_id', 'entity_type', 'completeness')[:SOURCE_LIMIT * 2])
    coverage_map = {(row['period_id'], row['entity_type']): row['completeness'] for row in coverage}
    if not rows or any(coverage_map.get((row.period_id, domain)) != 'complete'
                       for row in rows for domain in ('enrollment', 'final_result')):
        candidates.sources[-1].update(state='partial',
            message='Учебные сведения подтверждены не полностью. Отсутствие совпадения не означает отсутствия записи.')
    for row in rows:
        result = getattr(row, 'finalresult', None)
        # Existing accepted scope, explicit dataset equality, existing result formatter.
        result = result_values(result)['label'] if result and result.dataset_id == row.dataset_id else (
            'Итог не внесён' if coverage_map.get((row.period_id, 'final_result')) == 'complete' else 'Итог не подтверждён')
        candidates.items.append(entry('course:' + str(row.pk), row.course.name, 'Дисциплина',
            f'{row.period.name} · {result}', f'/study/enrollments/{row.pk}/', keywords='учеба оценка результат'))


def teaching(actor, now, candidates, revision):
    if not revision:
        candidates.source('teaching', 'Учебные публикации', 'unavailable',
            'Актуальность учебного источника не подтверждена; публикации по дисциплинам исключены из поиска.')
        return
    if actor.role == 'teacher':
        for row in candidates.take(assignment_scope(actor, now).select_related('course', 'period', 'group')
                .order_by('course__name', 'pk'), 'allocations', 'Мои дисциплины'):
            candidates.items.append(entry('allocation:' + str(row.pk), row.course.name, 'Моя дисциплина',
                f'{row.group.name} · {row.period.name}', f'/teacher/courses/{row.pk}/'))
    # Search deliberately includes published objects only, even for their author.
    query = teaching_scope(actor, now).filter(state='published', published_revision__isnull=False)
    query = query.select_related('published_revision', 'assignment__course', 'assignment__period').order_by('-updated_at', '-pk')
    for row in candidates.take(query, 'teaching', 'Материалы и задания'):
        payload = row.published_revision.payload
        category = 'Задание' if row.kind == 'task' else 'Материал преподавателя'
        prefix = '/teacher/publications/' if actor.role == 'teacher' else '/study/teaching/'
        candidates.items.append(entry('teaching:' + str(row.pk), payload.get('title', 'Публикация'), category,
            f'{row.assignment.course.name} · {row.assignment.period.name}', f'{prefix}{row.pk}/',
            keywords=str(payload.get('topic', ''))[:200]))


def requests(actor, candidates, query):
    scoped = request_scope(actor)
    exact = reference_id(query)
    if exact is not None:
        scoped = scoped.filter(pk=exact)
    # No comment, applicant snapshot or clarification text is read.
    rows = candidates.take(scoped.only('id', 'request_type', 'status', 'updated_at').order_by('-created_at', '-pk'),
                           'requests', 'Заявки')
    for row in rows:
        prefix = '/staff/requests/' if actor.role == 'staff' else '/requests/'
        candidates.items.append(entry('request:' + str(row.pk), f'{row.reference} · {REQUEST_TYPES[row.request_type]}',
            'Заявка', f'{STATUS_LABELS[row.status]} · {moscow(row.updated_at):%d.%m.%Y}', f'{prefix}{row.pk}/'))


def notices(actor, now, candidates):
    query = notice_scope(actor, now).filter(state='published', published_revision__isnull=False)
    for row in candidates.take(query.select_related('published_revision').order_by('-updated_at', '-pk'),
                               'faculty_notices', 'Объявления службы'):
        prefix = '/staff/notices/' if actor.role == 'staff' else '/notices/'
        candidates.items.append(entry('notice:' + str(row.pk), row.published_revision.payload.get('title', 'Объявление'),
            'Объявление службы', 'Доступная публикация факультета', f'{prefix}{row.pk}/'))
    if actor.role == 'student':
        for row in candidates.take(visible_announcements(actor, now=now).only('id', 'title', 'published_at')
                .order_by('-published_at', '-pk'), 'announcements', 'Учебные объявления'):
            candidates.items.append(entry('announcement:' + str(row.pk), row.title, 'Объявление',
                f'Опубликовано {moscow(row.published_at):%d.%m.%Y}', f'/announcements/{row.pk}/'))


def resources(actor, now, candidates):
    if actor.role == 'student':
        query = group_resources(actor, now)
    else:
        query = GroupResource.objects.filter(group__in=publisher_groups(actor), archived=False,
            available_from__lte=now, available_until__gt=now)
    for row in candidates.take(query.only('id', 'title', 'category', 'group_id').order_by('title', 'pk'),
                               'resources', 'Ресурсы группы'):
        url = '/profile/group/#group-resources' if actor.role == 'student' else f'/staff/resources/{row.pk}/'
        candidates.items.append(entry('resource:' + str(row.pk), row.title, 'Ресурс группы',
            row.get_category_display(), url))


def journeys(actor, candidates):
    if not apps.is_installed('journeys'):
        candidates.source('journeys', 'Мои ситуации', 'unavailable', 'Подготовка ситуаций временно недоступна.')
        return
    from journeys.selectors import journey_scope
    labels = {'exchange': 'Подготовиться к учебному обмену', 'correction': 'Проверить неточность в учебных сведениях'}
    rows = journey_scope(actor).only('id', 'template_key', 'version').order_by('-pk')
    for row in candidates.take(rows, 'journeys', 'Мои ситуации'):
        candidates.items.append(entry('journey:' + str(row.pk), labels[row.template_key], 'Моя ситуация',
            f'Сохранённая подготовка · редакция {row.version}', f'/journeys/{row.pk}/'))


def consultations(actor, now, candidates):
    if not apps.is_installed('consultations'):
        candidates.source('consultations', 'Консультации', 'unavailable', 'Запись на консультации временно недоступна.')
        return
    from consultations.selectors import discovery_records
    result = discovery_records(actor, now, limit=SOURCE_LIMIT)
    candidates.source('consultations', 'Консультации', 'partial' if result.get('partial') else 'ready',
        'Просмотрена ограниченная выборка записей; все доступные даты открываются в консультациях.' if result.get('partial') else '')
    for row in result['items'][:SOURCE_LIMIT]:
        candidates.items.append(entry('consultation:' + str(row['id']), row['title'], 'Консультация', row['context'], row['url']))


def search(actor, now, query, tokens, page=1):
    suggestions = actions(actor.role)
    if not query:
        return {'marker': actor.marker, 'state': 'ready', 'items': [], 'count': 0, 'page': 1, 'pages': 1,
            'page_size': PAGE_SIZE, 'partial': False, 'sources': [], 'suggestions': [public(item) for item in suggestions]}
    candidates = Candidates()
    candidates.items.extend(suggestions)
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
    exact = reference_id(query)
    matches = ([item for item in candidates.items if item['key'] == 'request:' + str(exact)]
               if exact is not None else ranked(candidates.items, query, tokens))
    count = len(matches)
    pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
    selected = min(page, pages)
    return {'marker': actor.marker, 'state': 'ready', 'items': [public(item) for item in matches[(selected - 1) * PAGE_SIZE:selected * PAGE_SIZE]],
        'count': count, 'page': selected, 'pages': pages, 'page_size': PAGE_SIZE,
        'partial': any(row['state'] != 'ready' for row in candidates.sources), 'sources': candidates.sources,
        'suggestions': [public(item) for item in suggestions]}
