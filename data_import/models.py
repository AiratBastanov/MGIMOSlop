from django.db import models
from django.db.models import Q, F

class Dataset(models.Model):
    source_id = models.CharField(max_length=80, primary_key=True)
    current_revision = models.PositiveIntegerField(default=0)
    state_hash = models.CharField(max_length=64, blank=True)

class ImportRevision(models.Model):
    dataset = models.ForeignKey(Dataset, on_delete=models.PROTECT)
    revision = models.PositiveIntegerField()
    parent_revision = models.PositiveIntegerField(null=True)
    content_hash = models.CharField(max_length=64)
    schema_version = models.PositiveSmallIntegerField()
    provenance = models.CharField(max_length=20, default='synthetic')
    prepared_by = models.CharField(max_length=100)
    preparation_reason = models.CharField(max_length=500)
    source_as_of = models.DateTimeField()
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()
    imported_at = models.DateTimeField()
    operator = models.CharField(max_length=100)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['dataset', 'revision'], name='revision_identity'),
            models.CheckConstraint(condition=Q(valid_until__gt=F('valid_from')), name='revision_interval'),
            models.CheckConstraint(condition=Q(source_as_of__lte=F('imported_at')), name='revision_source_time'),
            models.CheckConstraint(condition=Q(provenance='synthetic'), name='revision_synthetic'),
            models.CheckConstraint(condition=Q(revision__gte=1), name='revision_positive'),
        ]

class Coverage(models.Model):
    revision = models.ForeignKey(ImportRevision, on_delete=models.PROTECT)
    group = models.ForeignKey('studies.Group', on_delete=models.PROTECT)
    period = models.ForeignKey('studies.Period', on_delete=models.PROTECT)
    date_from = models.DateField()
    date_until = models.DateField()
    completeness = models.CharField(max_length=10)
    active_ids = models.JSONField(null=True)
    active_count = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['revision','group','period','date_from'], name='coverage_identity'),
            models.CheckConstraint(condition=Q(date_until__gt=F('date_from')), name='coverage_interval'),
            models.CheckConstraint(condition=Q(completeness__in=['complete','partial','unknown']), name='coverage_enum'),
            models.CheckConstraint(condition=(Q(completeness='complete', active_ids__isnull=False, active_count__isnull=False) | Q(completeness__in=['partial','unknown'], active_ids__isnull=True, active_count__isnull=True)), name='coverage_values'),
        ]

class ImportAttempt(models.Model):
    recorded_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(max_length=30)
    code = models.CharField(max_length=80)
    # Без пути файла, payload, connection string или raw exception.


class AcademicCoverage(models.Model):
    """Полнота конкретного учебного домена одного студента за период."""
    revision = models.ForeignKey(ImportRevision, on_delete=models.PROTECT)
    student = models.ForeignKey('studies.StudentProfile', on_delete=models.PROTECT)
    period = models.ForeignKey('studies.Period', on_delete=models.PROTECT)
    entity_type = models.CharField(max_length=20)
    completeness = models.CharField(max_length=10)
    record_ids = models.JSONField(null=True)
    record_count = models.PositiveIntegerField(null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['revision', 'student', 'period', 'entity_type'], name='academic_coverage_identity'),
            models.CheckConstraint(condition=Q(entity_type__in=['enrollment', 'final_result', 'requirement', 'assessment']), name='academic_coverage_domain'),
            models.CheckConstraint(condition=Q(completeness__in=['complete', 'partial', 'unknown']), name='academic_coverage_enum'),
            models.CheckConstraint(condition=(Q(completeness='complete', record_ids__isnull=False, record_count__isnull=False) |
                Q(completeness__in=['partial', 'unknown'], record_ids__isnull=True, record_count__isnull=True)), name='academic_coverage_values'),
        ]
