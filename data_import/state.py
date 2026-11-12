"""Материализация source-owned состояния для diff, integrity и свидетельств."""
from datetime import date, datetime, timezone
from .contracts import fields_for, DATASET_ID, digest
from .models import AcademicCoverage, Coverage, ImportRevision
from studies.models import (Period, Group, Course, Instructor, Lesson, ScheduleChange, Faculty, Programme,
    StudentProfile, ProfileChange, Enrollment, FinalResult, OutstandingRequirement, AssessmentEvent,
    EnrollmentChange, FinalResultChange, RequirementChange, AssessmentChange)
from notifications.models import Notification, Announcement, AnnouncementChange

MODELS = {'period':Period,'faculty':Faculty,'programme':Programme,'group':Group,'profile':StudentProfile,'course':Course,'instructor':Instructor,'lesson':Lesson,'announcement':Announcement,
    'enrollment':Enrollment,'final_result':FinalResult,'requirement':OutstandingRequirement,'assessment':AssessmentEvent}
RELATIONS = {'programme':['faculty'],'group':['programme'],'profile':['group'],'course':['period'], 'lesson':['group','course','period','instructor'], 'announcement':['faculty','group','student'],
    'enrollment':['student','course','period'],'final_result':['enrollment'],'requirement':['enrollment','result'],
    'assessment':['group','course','period','instructor']}
ACADEMIC_HISTORY = {'enrollment': (EnrollmentChange, 'enrollment'), 'final_result': (FinalResultChange, 'result'),
    'requirement': (RequirementChange, 'requirement'), 'assessment': (AssessmentChange, 'assessment')}

def current_schema():
    return ImportRevision.objects.filter(dataset_id=DATASET_ID).order_by('-revision').values_list('schema_version',flat=True).first() or 1

def scalar(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value

def records(schema_version=None):
    schema_version = schema_version or current_schema()
    allowed = fields_for(schema_version)
    result = {}
    for kind, model in MODELS.items():
        if kind not in allowed: continue
        result[kind] = {}
        query = model.objects.filter(dataset_id=DATASET_ID).select_related(*RELATIONS.get(kind,[])).order_by('source_id')
        for obj in query:
            row = {}
            for field in allowed[kind]:
                if field.endswith('_id') and field != 'source_id':
                    related = getattr(obj,field[:-3])
                    row[field] = related.source_id if related else None
                else:
                    row[field] = scalar(getattr(obj,field))
            result[kind][obj.source_id] = row
    return result

def canonical_state(schema_version=None):
    schema_version = schema_version or current_schema()
    result = {'records': records(schema_version)}
    # Внутренние PK исключены; восстановление может назначить другие surrogate IDs.
    query_specs = {
        'revisions': (ImportRevision.objects.filter(dataset_id=DATASET_ID).order_by('revision'),
                      ['revision','parent_revision','content_hash','schema_version','provenance','prepared_by','preparation_reason','source_as_of','valid_from','valid_until','imported_at','operator']),
        'coverage': (Coverage.objects.filter(revision__dataset_id=DATASET_ID).order_by('revision__revision','group__source_id','period__source_id','date_from'),
                     ['revision__revision','group__source_id','period__source_id','date_from','date_until','completeness','active_ids','active_count']),
        'changes': (ScheduleChange.objects.filter(dataset_id=DATASET_ID).order_by('event_id'),
                    ['event_id','revision__revision','lesson__source_id','change_type','before','after','reason','operator','source_updated_at','recorded_at']),
        'notifications': (Notification.objects.filter(schedule_change__dataset_id=DATASET_ID).order_by('schedule_change__event_id'),
                          ['schedule_change__event_id','group__source_id','published_at']),
    }
    if schema_version >= 2:
        query_specs['profile_changes'] = (ProfileChange.objects.filter(dataset_id=DATASET_ID).order_by('event_id'),
            ['event_id','revision__revision','profile__source_id','before','after','reason','operator','recorded_at'])
    if schema_version >= 3:
        query_specs['announcement_changes'] = (AnnouncementChange.objects.filter(dataset_id=DATASET_ID).order_by('event_id'),
            ['event_id','revision__revision','announcement__source_id','change_type','before','after','reason','operator','source_updated_at','recorded_at'])
        query_specs['announcement_notifications'] = (Notification.objects.filter(announcement_change__dataset_id=DATASET_ID).order_by('announcement_change__event_id'),
            ['announcement_change__event_id','published_at'])
    if schema_version >= 4:
        query_specs['academic_coverage'] = (AcademicCoverage.objects.filter(revision__dataset_id=DATASET_ID)
            .order_by('revision__revision','student__source_id','period__source_id','entity_type'),
            ['revision__revision','student__source_id','period__source_id','entity_type','completeness','record_ids','record_count'])
        for kind, (model, target) in ACADEMIC_HISTORY.items():
            query_specs[f'{kind}_changes'] = (model.objects.filter(dataset_id=DATASET_ID).order_by('event_id'),
                ['event_id','revision__revision',f'{target}__source_id','change_type','before','after','reason','operator','source_updated_at','recorded_at'])
            if kind != 'enrollment':
                field = f'{kind}_change'
                query_specs[f'{kind}_notifications'] = (Notification.objects.filter(**{f'{field}__dataset_id':DATASET_ID})
                    .order_by(f'{field}__event_id'), [f'{field}__event_id','published_at'])
    for key, (query, field_names) in query_specs.items():
        result[key] = [{k:scalar(v) for k,v in row.items()} for row in query.values(*field_names)]
    return result

def state_hash(schema_version=None):
    return digest(canonical_state(schema_version))
