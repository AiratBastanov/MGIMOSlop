from django.db import models
from django.db.models import F, Q
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators

class SourceRecord(models.Model):
    dataset = models.ForeignKey('data_import.Dataset', on_delete=models.PROTECT)
    source_id = models.CharField(max_length=80)
    record_revision = models.PositiveIntegerField()
    source_updated_at = models.DateTimeField()

    class Meta:
        abstract = True
        constraints = [
            models.UniqueConstraint(fields=['dataset','source_id'], name='%(class)s_source_identity'),
            models.CheckConstraint(condition=Q(record_revision__gte=1), name='%(class)s_positive_revision'),
        ]

class Period(SourceRecord):
    name = models.CharField(max_length=100)
    date_from = models.DateField()
    date_until = models.DateField()

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(date_until__gt=F('date_from')), name='period_interval'),
            models.CheckConstraint(condition=~Q(name=''), name='period_name'),
        ]

class Group(SourceRecord):
    name = models.CharField(max_length=100)
    programme = models.ForeignKey('Programme', null=True, on_delete=models.PROTECT)

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [models.CheckConstraint(condition=~Q(name=''), name='group_name')]

class Course(SourceRecord):
    name = models.CharField(max_length=120)
    period = models.ForeignKey(Period, on_delete=models.PROTECT)

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [models.CheckConstraint(condition=~Q(name=''), name='course_name')]

class Instructor(SourceRecord):
    name = models.CharField(max_length=100)

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [models.CheckConstraint(condition=~Q(name=''), name='instructor_name')]

class Lesson(SourceRecord):
    group = models.ForeignKey(Group, on_delete=models.PROTECT)
    course = models.ForeignKey(Course, on_delete=models.PROTECT)
    period = models.ForeignKey(Period, on_delete=models.PROTECT)
    instructor = models.ForeignKey(Instructor, null=True, on_delete=models.PROTECT)
    instructor_status = models.CharField(max_length=20)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=10)
    room = models.CharField(max_length=60, null=True)
    location_status = models.CharField(max_length=20)

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(ends_at__gt=F('starts_at')), name='lesson_interval'),
            models.CheckConstraint(condition=Q(status__in=['active','cancelled']), name='lesson_status'),
            models.CheckConstraint(condition=(Q(instructor_status='provided', instructor__isnull=False) | Q(instructor_status='not_provided', instructor__isnull=True)), name='lesson_instructor'),
            models.CheckConstraint(condition=(Q(location_status='provided',room__isnull=False) & ~Q(room='') | Q(location_status='not_provided',room__isnull=True)), name='lesson_location'),
            ExclusionConstraint(name='lesson_group_overlap', expressions=[
                ('group', RangeOperators.EQUAL),
                (models.Func('starts_at','ends_at',models.Value('[)'), function='TSTZRANGE',output_field=DateTimeRangeField()),RangeOperators.OVERLAPS),
            ], condition=Q(status='active'), deferrable=models.Deferrable.DEFERRED),
            ExclusionConstraint(name='lesson_instructor_overlap', expressions=[
                ('instructor', RangeOperators.EQUAL),
                (models.Func('starts_at','ends_at',models.Value('[)'), function='TSTZRANGE',output_field=DateTimeRangeField()),RangeOperators.OVERLAPS),
            ], condition=Q(status='active',instructor__isnull=False), deferrable=models.Deferrable.DEFERRED),
        ]

class ScheduleChange(models.Model):
    dataset = models.ForeignKey('data_import.Dataset', on_delete=models.PROTECT)
    revision = models.ForeignKey('data_import.ImportRevision', on_delete=models.PROTECT)
    event_id = models.CharField(max_length=80)
    lesson = models.ForeignKey(Lesson, on_delete=models.PROTECT)
    change_type = models.CharField(max_length=20)
    before = models.JSONField()
    after = models.JSONField()
    reason = models.CharField(max_length=500)
    operator = models.CharField(max_length=100)
    source_updated_at = models.DateTimeField()
    recorded_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['dataset','event_id'], name='schedule_event_identity'),
            models.CheckConstraint(condition=Q(change_type__in=['correction','move','cancel','substitute']), name='schedule_change_type'),
        ]


class Faculty(SourceRecord):
    name = models.CharField(max_length=100)


class Programme(SourceRecord):
    name = models.CharField(max_length=100)
    faculty = models.ForeignKey(Faculty, on_delete=models.PROTECT)


class StudentProfile(SourceRecord):
    name = models.CharField(max_length=100)
    group = models.ForeignKey(Group, on_delete=models.PROTECT)
    year = models.PositiveSmallIntegerField()
    scope_revision = models.PositiveBigIntegerField(default=1)

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [models.CheckConstraint(condition=Q(year__gte=1,year__lte=6), name='profile_year_range')]


class ProfileChange(models.Model):
    dataset = models.ForeignKey('data_import.Dataset', on_delete=models.PROTECT)
    revision = models.ForeignKey('data_import.ImportRevision', on_delete=models.PROTECT)
    profile = models.ForeignKey(StudentProfile, on_delete=models.PROTECT)
    event_id = models.CharField(max_length=80)
    before = models.JSONField()
    after = models.JSONField()
    reason = models.CharField(max_length=500)
    operator = models.CharField(max_length=100)
    recorded_at = models.DateTimeField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['dataset','event_id'], name='profile_event_identity')]


class Enrollment(SourceRecord):
    student = models.ForeignKey(StudentProfile, on_delete=models.PROTECT)
    course = models.ForeignKey(Course, on_delete=models.PROTECT)
    period = models.ForeignKey(Period, on_delete=models.PROTECT)
    status = models.CharField(max_length=12)

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [
            models.UniqueConstraint(fields=['student', 'course', 'period'], name='enrollment_student_course_period'),
            models.CheckConstraint(condition=Q(status__in=['active', 'withdrawn']), name='enrollment_status'),
        ]


class FinalResult(SourceRecord):
    enrollment = models.OneToOneField(Enrollment, on_delete=models.PROTECT)
    result_kind = models.CharField(max_length=12)
    grade = models.PositiveSmallIntegerField(null=True)
    pass_status = models.CharField(max_length=10, null=True)
    recorded_at = models.DateTimeField()

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [
            models.CheckConstraint(condition=(
                Q(result_kind='grade', grade__isnull=False, grade__gte=2, grade__lte=5, pass_status__isnull=True) |
                Q(result_kind='pass_fail', grade__isnull=True, pass_status__isnull=False, pass_status__in=['passed', 'failed'])
            ), name='final_result_value'),
            models.CheckConstraint(condition=Q(recorded_at__lte=F('source_updated_at')), name='final_result_source_time'),
        ]


class OutstandingRequirement(SourceRecord):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.PROTECT)
    result = models.ForeignKey(FinalResult, null=True, on_delete=models.PROTECT)
    reason = models.CharField(max_length=500)
    status = models.CharField(max_length=10)
    opened_at = models.DateTimeField()
    due_at = models.DateTimeField(null=True)
    closed_at = models.DateTimeField(null=True)

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [
            models.CheckConstraint(condition=~Q(reason=''), name='requirement_reason'),
            models.CheckConstraint(condition=(Q(status='open', closed_at__isnull=True) |
                Q(status='closed', closed_at__isnull=False, closed_at__gte=F('opened_at'))), name='requirement_state'),
            models.CheckConstraint(condition=Q(due_at__isnull=True) | Q(due_at__gte=F('opened_at')), name='requirement_due_interval'),
            models.CheckConstraint(condition=Q(opened_at__lte=F('source_updated_at')) &
                (Q(closed_at__isnull=True) | Q(closed_at__lte=F('source_updated_at'))), name='requirement_source_time'),
        ]


class AssessmentEvent(SourceRecord):
    group = models.ForeignKey(Group, on_delete=models.PROTECT)
    course = models.ForeignKey(Course, on_delete=models.PROTECT)
    period = models.ForeignKey(Period, on_delete=models.PROTECT)
    assessment_kind = models.CharField(max_length=12)
    instructor = models.ForeignKey(Instructor, null=True, on_delete=models.PROTECT)
    instructor_status = models.CharField(max_length=20)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=10)
    room = models.CharField(max_length=60, null=True)
    location_status = models.CharField(max_length=20)

    class Meta(SourceRecord.Meta):
        constraints = SourceRecord.Meta.constraints + [
            models.CheckConstraint(condition=Q(assessment_kind__in=['exam', 'pass_fail']), name='assessment_kind'),
            models.CheckConstraint(condition=Q(ends_at__gt=F('starts_at')), name='assessment_interval'),
            models.CheckConstraint(condition=Q(status__in=['active', 'cancelled']), name='assessment_status'),
            models.CheckConstraint(condition=(Q(instructor_status='provided', instructor__isnull=False) |
                Q(instructor_status='not_provided', instructor__isnull=True)), name='assessment_instructor'),
            models.CheckConstraint(condition=(Q(location_status='provided', room__isnull=False) & ~Q(room='') |
                Q(location_status='not_provided', room__isnull=True)), name='assessment_location'),
            ExclusionConstraint(name='assessment_group_overlap', expressions=[
                ('group', RangeOperators.EQUAL),
                (models.Func('starts_at', 'ends_at', models.Value('[)'), function='TSTZRANGE', output_field=DateTimeRangeField()), RangeOperators.OVERLAPS),
            ], condition=Q(status='active'), deferrable=models.Deferrable.DEFERRED),
            ExclusionConstraint(name='assessment_instructor_overlap', expressions=[
                ('instructor', RangeOperators.EQUAL),
                (models.Func('starts_at', 'ends_at', models.Value('[)'), function='TSTZRANGE', output_field=DateTimeRangeField()), RangeOperators.OVERLAPS),
            ], condition=Q(status='active', instructor__isnull=False), deferrable=models.Deferrable.DEFERRED),
        ]


class AcademicChange(models.Model):
    dataset = models.ForeignKey('data_import.Dataset', on_delete=models.PROTECT)
    revision = models.ForeignKey('data_import.ImportRevision', on_delete=models.PROTECT)
    event_id = models.CharField(max_length=80)
    change_type = models.CharField(max_length=20)
    before = models.JSONField(null=True)
    after = models.JSONField()
    reason = models.CharField(max_length=500)
    operator = models.CharField(max_length=100)
    source_updated_at = models.DateTimeField()
    recorded_at = models.DateTimeField()

    class Meta:
        abstract = True
        constraints = [models.UniqueConstraint(fields=['dataset', 'event_id'], name='%(class)s_event_identity')]


class EnrollmentChange(AcademicChange):
    enrollment = models.ForeignKey(Enrollment, on_delete=models.PROTECT)

    class Meta(AcademicChange.Meta):
        constraints = AcademicChange.Meta.constraints + [
            models.CheckConstraint(condition=Q(change_type__in=['create', 'correction']), name='enrollment_change_type')]


class FinalResultChange(AcademicChange):
    result = models.ForeignKey(FinalResult, on_delete=models.PROTECT)

    class Meta(AcademicChange.Meta):
        constraints = AcademicChange.Meta.constraints + [
            models.CheckConstraint(condition=Q(change_type__in=['create', 'correction']), name='final_result_change_type')]


class RequirementChange(AcademicChange):
    requirement = models.ForeignKey(OutstandingRequirement, on_delete=models.PROTECT)

    class Meta(AcademicChange.Meta):
        constraints = AcademicChange.Meta.constraints + [
            models.CheckConstraint(condition=Q(change_type__in=['create', 'correction', 'close', 'reopen']), name='requirement_change_type')]


class AssessmentChange(AcademicChange):
    assessment = models.ForeignKey(AssessmentEvent, on_delete=models.PROTECT)

    class Meta(AcademicChange.Meta):
        constraints = AcademicChange.Meta.constraints + [
            models.CheckConstraint(condition=Q(change_type__in=['create', 'correction', 'move', 'cancel', 'substitute']), name='assessment_change_type')]
