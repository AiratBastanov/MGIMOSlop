"""Один scope для страниц, количества, чтения и команд материалов."""
from django.db.models import Exists, OuterRef, F, Max
from accounts.access import AccessDenied
from studies.models import Enrollment
from studies.selectors import moscow
from .models import TeachingAssignment, SubmissionPolicy, Material, MaterialRevision

MODE_LABELS = {'text': 'Текст', 'link': 'Ссылка на облако', 'email': 'Контактная почта'}
REVIEW_LABELS = {'accepted': 'Принято', 'changes_requested': 'Нужны исправления'}
CLOUD_HOSTS = ('drive.google.com', 'docs.google.com', 'disk.yandex.ru', '1drv.ms', 'onedrive.live.com')


def assignment_candidates(actor):
    """Scope без времени: также нужен privacy deadline ближайшей границы."""
    query = TeachingAssignment.objects.filter(active=True, teacher__role='teacher', teacher__user__is_active=True,
        teacher__user__is_staff=False, teacher__user__is_superuser=False,
        teacher__user__groups__isnull=True, teacher__user__user_permissions__isnull=True,
        teacher__staffscope__isnull=True, teacher__profile__isnull=True,
        course__period_id=F('period_id'), course__dataset_id=F('period__dataset_id'),
        group__dataset_id=F('period__dataset_id'), teacher__instructor__dataset_id=F('period__dataset_id'))
    if actor.role == 'teacher':
        return query.filter(teacher=actor.identity)
    if actor.role == 'student':
        enrolled = Enrollment.objects.filter(student_id=actor.profile_id, status='active',
            course_id=OuterRef('course_id'), period_id=OuterRef('period_id'),
            dataset_id=actor.identity.profile.dataset_id)
        return query.filter(group_id=actor.group_id).annotate(enrolled=Exists(enrolled)).filter(enrolled=True)
    raise AccessDenied('forbidden', 403)


def assignment_scope(actor, now):
    return assignment_candidates(actor).filter(available_from__lte=now,available_until__gt=now)


def material_scope(actor, now):
    if actor.role == 'student':
        return Material.objects.filter(owner_id=actor.profile_id)
    if actor.role == 'teacher':
        enrollment = Enrollment.objects.filter(student_id=OuterRef('owner_id'), status='active',
            course_id=OuterRef('assignment__course_id'), period_id=OuterRef('assignment__period_id'),
            dataset_id=OuterRef('owner__dataset_id'))
        return Material.objects.filter(assignment__in=assignment_scope(actor, now),
            submission_count__gt=0, owner__group_id=F('assignment__group_id'),
            owner__dataset_id=F('assignment__course__dataset_id')).annotate(
                enrolled=Exists(enrollment)).filter(enrolled=True)
    raise AccessDenied('forbidden', 403)


def revision_scope(actor, now):
    return MaterialRevision.objects.filter(material__in=material_scope(actor, now))


def current_policy(assignment_id, task_id=None):
    query=SubmissionPolicy.objects.filter(assignment_id=assignment_id)
    if task_id:
        from workspaces.models import CourseTask
        revision=CourseTask.objects.filter(pk=task_id).values('publication__published_revision_id')
        return query.filter(task_revision_id__in=revision).first()
    return query.filter(task_revision__isnull=True).order_by('-version').first()


def policy_values(policy, *, effective=False):
    if not policy:
        return None
    result={'id': policy.pk, 'version': policy.version, 'modes': list(policy.modes),
        'mode_labels': [MODE_LABELS[mode] for mode in policy.modes],
        'text_limit': policy.text_limit, 'cloud_hosts': list(policy.cloud_hosts),
        'instructions': policy.instructions, 'created_at': moscow(policy.created_at),
        'attachments_allowed':policy.attachments_allowed,'attachment_types':list(policy.attachment_types),
        'attachment_count':policy.attachment_count,'attachment_file_limit':policy.attachment_file_limit,
        'attachment_total_limit':policy.attachment_total_limit}
    if effective:
        from .attachments import limits
        result['attachment_count'],result['attachment_file_limit'],result['attachment_total_limit']=limits(policy)
    return result


def assignment_values(row, actor=None):
    from profiles.selectors import person_for_identity
    return {'id': row.pk, 'course': row.course.name, 'group': row.group.name,
        'period': row.period.name, 'teacher': row.teacher.instructor.name,
        'teacher_person':person_for_identity(actor,row.teacher) if actor else {'name':row.teacher.instructor.name,'avatar_url':''},
        'teacher_user_id': row.teacher.user_id, 'instructor_id': row.teacher.instructor_id,
        'available_until': moscow(row.available_until), 'policy': policy_values(current_policy(row.pk),effective=True),
        'settings_url': f'/teacher/assignments/{row.pk}/'}


def assignment_choices(actor, now):
    rows = assignment_scope(actor, now).select_related('course','group','period','teacher__instructor').order_by('course__name','pk')
    return [assignment_values(row,actor) for row in rows]


def material_values(actor, id, now):
    from .attachments import file_values
    from profiles.selectors import person_for_identity
    from accounts.models import Identity
    row = material_scope(actor, now).select_related('assignment__course','assignment__group',
        'assignment__period','assignment__teacher__instructor','owner').filter(pk=id).first()
    if row is None:
        raise AccessDenied('not_found',404)
    revisions = []
    for revision in row.revisions.select_related('policy','review__teacher__instructor').order_by('-number'):
        review = getattr(revision,'review',None)
        revisions.append({'id':revision.pk, 'number':revision.number, 'title':revision.title,
            'mode':revision.mode, 'mode_label':MODE_LABELS[revision.mode], 'content':revision.content,
            'submitted_at':moscow(revision.submitted_at), 'task_conditions':revision.task_conditions, 'task_revision_id':revision.task_revision_id,
            'task_revision_number':revision.task_revision.number if revision.task_revision_id else None,'policy':policy_values(revision.policy),
            'attachments':[file_values(f.attachment,revision.pk) for f in revision.files.select_related('attachment').defer('attachment__data').order_by('position')],
            'review': {'status':review.status, 'status_label':REVIEW_LABELS[review.status],
                'feedback':review.feedback,'result':review.result,'reviewed_at':moscow(review.reviewed_at),
                'teacher':review.teacher.instructor.name, 'teacher_user_id':review.teacher.user_id,
                'person':person_for_identity(actor,review.teacher)} if review else None})
    assignment = assignment_values(row.assignment,actor)
    task_visible=False
    if row.task_id:
        from workspaces.selectors import task_scope
        task_visible=task_scope(actor,now).filter(pk=row.task_id).exists()
        latest=row.revisions.select_related('policy').order_by('-number').first()
        assignment['policy']=policy_values(current_policy(row.assignment_id,row.task_id) if task_visible else latest.policy if latest else None,effective=True)
    can_submit = actor.role == 'student' and assignment_scope(actor,now).filter(pk=row.assignment_id).exists() and assignment['policy'] is not None
    if row.task_id: can_submit=can_submit and task_visible
    owner_identity=Identity.objects.select_related('profile','user').filter(profile_id=row.owner_id).first()
    item = {'id':row.pk,'assignment':assignment,'owner_name':row.owner.name,'owner_id':row.owner_id,
        'owner_person':person_for_identity(actor,owner_identity) if owner_identity else {'name':row.owner.name,'avatar_url':''},
        'revisions':revisions,'submitted_count':row.submission_count,'can_submit':can_submit,
        'url': f'/study/materials/{row.pk}/', 'teacher_url':f'/teacher/materials/{row.pk}/',
        'updated_at':moscow(row.updated_at) if actor.role=='student' else revisions[0]['submitted_at'],
        'task_id':row.task_id,'task_revision_id':row.task.publication.published_revision_id if task_visible else None,
        'task_url':f'/study/teaching/{row.task.publication_id}/' if task_visible else '',
        'task_title':row.task.publication.published_revision.payload['title'] if task_visible else 'Задание недоступно' if row.task_id else '',
        'assessment_mode':row.task.publication.published_revision.payload['assessment_mode'] if task_visible else 'none'}
    if actor.role == 'student':
        item.update(draft_title=row.draft_title,draft_mode=row.draft_mode,draft_content=row.draft_content,
            draft_version=row.draft_version,has_draft=row.has_draft,
            attachments=[file_values(f.attachment) for f in row.draft_files.select_related('attachment').defer('attachment__data').order_by('position')])
    return item


def workspace(actor, now):
    from profiles.selectors import person_for_identity
    from accounts.models import Identity
    assignments = assignment_choices(actor,now)
    rows = material_scope(actor,now).select_related('assignment__course','owner').order_by('-updated_at','-pk')
    if actor.role=='teacher':
        rows=rows.annotate(last_submitted=Max('revisions__submitted_at')).order_by('-last_submitted','-pk')
    items = []
    for row in rows:
        latest = row.revisions.select_related('review').order_by('-number').first()
        review = getattr(latest,'review',None) if latest else None
        owner_identity=Identity.objects.select_related('profile','user').filter(profile_id=row.owner_id).first()
        item = {'id':row.pk,'course':row.assignment.course.name,'owner_name':row.owner.name,
            'owner_person':person_for_identity(actor,owner_identity) if owner_identity else {'name':row.owner.name,'avatar_url':''},
            'title': row.draft_title if actor.role == 'student' and row.has_draft else latest.title,
            'url':f'/study/materials/{row.pk}/' if actor.role == 'student' else f'/teacher/materials/{row.pk}/',
            'status': 'Черновик' if actor.role == 'student' and row.has_draft else (REVIEW_LABELS[review.status] if review else 'Ожидает отзыва'),
            'number':latest.number if latest else None,
            'updated_at':moscow(row.updated_at if actor.role=='student' else latest.submitted_at)}
        items.append(item)
    return {'assignments':assignments,'materials':items}
