from django.db.models import Q
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from urllib.parse import quote
import re
from accounts.access import AccessDenied
from accounts.models import Identity, StaffScope
from studies.models import Group, StudentProfile, Enrollment
from .models import Preferences, Avatar, GroupResource


def can_see_person(actor, identity):
    if actor.user_id == identity.user_id:
        return True
    pref = Preferences.objects.filter(user_id=identity.user_id, photo_audience='context').exists()
    if not pref or not identity.user.is_active:
        return False
    if actor.role == 'student' and identity.role == 'student':
        return identity.profile.group_id == actor.group_id and identity.profile.dataset_id == actor.identity.profile.dataset_id
    now = actor.checked_at
    # Assignment field names and time predicates are provided by the materials selector.
    from materials.selectors import assignment_scope
    if actor.role == 'student' and identity.role == 'teacher':
        return assignment_scope(actor, now=now).filter(teacher=identity).exists()
    if actor.role == 'teacher' and identity.role == 'student':
        from django.db.models import Exists, OuterRef
        enrollment = Enrollment.objects.filter(student_id=identity.profile_id, status='active',
            course_id=OuterRef('course_id'), period_id=OuterRef('period_id'), dataset_id=identity.profile.dataset_id)
        return assignment_scope(actor, now=now).filter(group_id=identity.profile.group_id,
            course__dataset_id=identity.profile.dataset_id).annotate(current_enrollment=Exists(enrollment)).filter(current_enrollment=True).exists()
    return False


def person_for_identity(actor, identity):
    name = identity.profile.name if identity.role == 'student' else identity.instructor.name if identity.role == 'teacher' else identity.display_name
    photo = Avatar.objects.filter(identity=identity).only('digest').first() if can_see_person(actor, identity) else None
    return {'name': name, 'avatar_url': f'/people/{identity.pk}/photo/?v={photo.digest[:16]}' if photo else ''}


def person_for_instructor(actor, instructor_id, name):
    identity = Identity.objects.filter(role='teacher', instructor_id=instructor_id).select_related('instructor', 'user').first()
    return person_for_identity(actor, identity) if identity else {'name': name, 'avatar_url': ''}


def own_preferences(actor):
    return Preferences.objects.filter(user_id=actor.user_id).first() or Preferences(user_id=actor.user_id, version=0)


def publisher_groups(actor):
    if actor.role != 'staff':
        raise AccessDenied('forbidden', 403)
    scope = StaffScope.objects.get(identity=actor.identity)
    return Group.objects.filter(programme__faculty_id=scope.faculty_id).order_by('name', 'pk')


def group_directory(actor):
    if actor.role != 'student':
        raise AccessDenied('forbidden', 403)
    result = []
    for profile in StudentProfile.objects.filter(group_id=actor.group_id, dataset_id=actor.identity.profile.dataset_id).exclude(pk=actor.profile_id).order_by('name', 'pk'):
        identity = Identity.objects.filter(profile=profile, role='student', user__is_active=True).select_related('profile', 'user').first()
        person=person_for_identity(actor, identity) if identity else {'name': profile.name, 'avatar_url': ''}
        pref=Preferences.objects.filter(user_id=identity.user_id).first() if identity else None
        # Never add directory data to the cross-product person selector.
        if pref and pref.share_email and pref.email_verified_at and pref.contact_email:
            try:
                validate_email(pref.contact_email)
            except ValidationError:
                pass
            else:
                person['directory_email']=pref.contact_email
                person['directory_email_href']='mailto:'+quote(pref.contact_email, safe='@')
        if pref and pref.share_phone and re.fullmatch(r'\+?[0-9]{7,15}',pref.directory_phone):
            person['directory_phone']=pref.directory_phone
            person['directory_phone_href']='tel:'+pref.directory_phone
        result.append(person)
    return result


def group_resources(actor, now):
    if actor.role != 'student':
        raise AccessDenied('forbidden', 403)
    return GroupResource.objects.filter(group_id=actor.group_id, archived=False, available_from__lte=now,
        available_until__gt=now).order_by('category', '-available_from', '-pk')
