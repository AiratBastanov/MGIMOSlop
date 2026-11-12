import copy
from datetime import date, datetime, timedelta, timezone as utc
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, IntegrityError, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from data_import.contracts import DATASET_ID, ImportRejected, SourceUnavailable, validate, content_hash
from data_import.models import Dataset, ImportRevision, Coverage, ImportAttempt
from data_import.provider import SyntheticDatasetProvider
from data_import import services
from data_import.services import apply, preview, initialize
from studies.models import Lesson, ScheduleChange, Group, Instructor
from notifications.models import Notification
from studies.selectors import operator_schedule, operator_snapshot

ROOT = Path(__file__).resolve().parents[1]

def payload(name):
    return json.loads((ROOT / 'fixtures' / 's1' / name).read_text(encoding='utf-8'))

def signed(data):
    # Независимая реализация спецификации checksum для изменённых негативных кандидатов.
    data = copy.deepcopy(data)
    covered = {key:value for key,value in data.items() if key != 'content_sha256'}
    data['content_sha256'] = hashlib.sha256(json.dumps(covered,ensure_ascii=False,sort_keys=True,separators=(',', ':'),allow_nan=False).encode('utf-8')).hexdigest()
    return data

class ImportSliceTests(TransactionTestCase):
    def setUp(self):
        initialize()
        self.r1 = SyntheticDatasetProvider().load(ROOT / 'fixtures/s1/r1.json')
        self.r2 = SyntheticDatasetProvider().load(ROOT / 'fixtures/s1/r2.json')

    def initial(self):
        return apply(self.r1,expected_revision=0,operator='test-operator')

    def updated(self):
        self.initial()
        return apply(self.r2,expected_revision=1,operator='test-operator')

    def snapshot(self):
        return operator_snapshot()

    def read(self, day=23):
        return operator_schedule(group_id='G-S1',period_id='P-2026-autumn',date=date(2026,11,day))

    def test_postgres_migrations_and_restricted_role(self):
        with connection.cursor() as cursor:
            cursor.execute('SELECT current_database(), current_user, current_setting(\'server_version_num\')::int')
            db,user,version = cursor.fetchone()
            self.assertEqual((db,user),('deanery_test','deanery_test'))
            self.assertTrue(170000 <= version < 180000)
            cursor.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user')
            self.assertEqual(cursor.fetchone(),(False,False,False))
            cursor.execute("SELECT has_database_privilege(current_user,'deanery_dev','CONNECT')")
            self.assertEqual(cursor.fetchone(),(False,))
        self.assertEqual(self.client.get('/health/ready/').status_code,200)
        self.assertEqual(self.read()['state'],'not_prepared')

    def test_provider_hash_and_preview_have_no_writes(self):
        before = self.snapshot()
        with self.assertNumQueries(0):
            batch = SyntheticDatasetProvider().load(ROOT / 'fixtures/s1/r1.json')
        self.assertEqual(batch.payload['content_sha256'],'9ba34a3634804294e42db056f48328b815c2e211e5c4bafbe9fbf64d5393f60a')
        self.assertEqual(content_hash({'a':'Я','content_sha256':'ignored'}),hashlib.sha256('{"a":"Я"}'.encode()).hexdigest())
        result = preview(batch,expected_revision=0)
        self.assertEqual(result['outcome'],'preview')
        self.assertEqual(len(result['changes']),5)
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(ImportAttempt.objects.count(),0)

    def test_exact_initial_and_move_history_notification(self):
        self.initial()
        lesson = Lesson.objects.get(source_id='L-S1-23')
        self.assertEqual(lesson.room,'204')
        self.assertEqual(lesson.starts_at,datetime(2026,11,23,6,0,tzinfo=utc.utc))
        self.assertEqual(lesson.ends_at,datetime(2026,11,23,7,30,tzinfo=utc.utc))
        self.assertEqual(lesson.group.name,'ДЕМО-21')
        self.assertEqual(lesson.course.name,'Прикладная экономика (демо)')
        self.assertEqual(lesson.instructor.name,'Олег Лесной (вымышленный)')
        self.assertEqual(ScheduleChange.objects.count(),0)
        before_time = timezone.now()
        apply(self.r2,expected_revision=1,operator='test-operator')
        lesson.refresh_from_db()
        self.assertEqual((lesson.room,lesson.record_revision),('318',2))
        self.assertEqual(lesson.starts_at,datetime(2026,11,23,7,40,tzinfo=utc.utc))
        self.assertEqual(lesson.ends_at,datetime(2026,11,23,9,10,tzinfo=utc.utc))
        change = ScheduleChange.objects.get()
        self.assertEqual(change.event_id,'EV-S1-MOVE')
        self.assertEqual((change.before['room'],change.after['room']),('204','318'))
        self.assertEqual(change.before['starts_at'],'2026-11-23T06:00:00+00:00')
        self.assertEqual(change.after['starts_at'],'2026-11-23T07:40:00+00:00')
        self.assertGreaterEqual(change.recorded_at,before_time)
        notification = Notification.objects.get()
        self.assertEqual(notification.schedule_change_id,change.pk)
        self.assertEqual(notification.group_id,lesson.group_id)
        self.assertEqual(ImportRevision.objects.count(),2)
        self.assertEqual(self.read()['lessons'][0]['starts_at_moscow'],'2026-11-23T10:40:00+03:00')

    def test_invalid_structure_references_intervals_and_scope_leave_state(self):
        self.initial()
        before = self.snapshot()
        cases = []
        def case(edit):
            value = payload('r2.json'); edit(value); cases.append(signed(value))
        case(lambda x:x.update(schema_version=99))
        case(lambda x:x.update(password='forbidden'))
        case(lambda x:x['operations'][0].update(entity_type='enrollment'))
        case(lambda x:x['operations'][0].update(operation='delete'))
        case(lambda x:x['operations'][0]['after'].update(instructor_id='missing'))
        case(lambda x:x['operations'][0]['after'].update(group_id='G-OTHER'))
        case(lambda x:x['operations'][0]['after'].update(ends_at='2026-11-23T10:00:00+03:00'))
        case(lambda x:x['operations'][0]['after'].update(starts_at='2026-11-23T10:40:00'))
        case(lambda x:x['operations'].append(copy.deepcopy(x['operations'][0])))
        case(lambda x:x['operations'][0]['after'].update(role='staff'))
        case(lambda x:x['operations'][0]['before'].update(room='999'))
        case(lambda x:x['coverage'][0].update(active_ids=[],active_count=0))
        case(lambda x:x['coverage'][0].update(active_count=2))
        case(lambda x:x.update(valid_until=x['valid_from']))
        case(lambda x:x.update(source_as_of='2099-01-01T00:00:00+00:00'))
        for index,candidate in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(ImportRejected):
                    apply(validate(candidate),expected_revision=1,operator='test')
                self.assertEqual(self.snapshot(),before)

    def test_retained_active_lesson_cannot_be_hidden_by_empty_complete_coverage(self):
        self.initial()
        candidate = payload('r2.json')
        candidate['operations'] = []
        candidate['coverage'][0].update(active_ids=[],active_count=0)
        before = self.snapshot()
        with self.assertRaisesRegex(ImportRejected,'итоговыми активными'):
            apply(validate(signed(candidate)),expected_revision=1,operator='test')
        self.assertEqual(self.snapshot(),before)

    def test_schema_rejects_duplicate_json_keys_size_and_missing_source(self):
        provider = SyntheticDatasetProvider()
        with tempfile.TemporaryDirectory(dir=ROOT / '.local') as directory:
            path = Path(directory) / 'invalid.json'
            for raw in [b'{"schema_version":1,"schema_version":1}',b'\xff', b' '*(2*1024*1024+1),b'{"a":NaN}']:
                path.write_bytes(raw)
                with self.assertRaises(ImportRejected): provider.load(path)
            with self.assertRaises(SourceUnavailable): provider.load(Path(directory)/'absent.json')

    def test_replay_conflict_stale_parent_older_revision_and_preview_revalidation(self):
        self.initial()
        preview(self.r2,expected_revision=1)
        apply(self.r2,expected_revision=1,operator='test')
        before = self.snapshot()
        self.assertEqual(apply(self.r2,expected_revision=2,operator='test')['outcome'],'noop')
        candidate = payload('r2.json'); candidate['preparation_reason'] = 'Иное содержимое'
        with self.assertRaises(ImportRejected): apply(validate(signed(candidate)),expected_revision=2,operator='test')
        with self.assertRaises(ImportRejected): apply(self.r1,expected_revision=2,operator='test')
        with self.assertRaises(ImportRejected): apply(self.r2,expected_revision=1,operator='test')
        candidate = payload('r2.json'); candidate.update(dataset_revision=4,parent_revision=3)
        with self.assertRaisesRegex(ImportRejected,'Parent'): apply(validate(signed(candidate)),expected_revision=2,operator='test')
        self.assertEqual(self.snapshot(),before)

    def test_integrity_checked_before_identical_replay(self):
        self.updated()
        Lesson.objects.update(room='unauthorized')
        with self.assertRaisesRegex(ImportRejected,'отпечатка'):
            apply(self.r2,expected_revision=2,operator='test')
        self.assertEqual(Lesson.objects.get().room,'unauthorized')  # ошибка не маскируется reset

    def test_schema_upgrade_cannot_adopt_unimported_v2_record(self):
        self.updated()
        from studies.models import Faculty
        Faculty.objects.create(dataset_id=DATASET_ID,source_id='UNIMPORTED',record_revision=1,source_updated_at=timezone.now(),name='Не принятый факультет')
        batch=SyntheticDatasetProvider().load(ROOT/'fixtures/s2/r3.json')
        with self.assertRaisesRegex(ImportRejected,'пусты'):
            apply(batch,expected_revision=2,operator='test')
        self.assertEqual(Dataset.objects.get().current_revision,2)

    def test_initialization_and_import_preserve_framework_state(self):
        user = User.objects.create_user(username='fictional-control',password='test-only-password')
        session = Session.objects.create(session_key='s1-control-session',session_data='opaque-control',expire_date=timezone.now()+timedelta(hours=1))
        password_hash = user.password
        self.updated()
        before = self.snapshot()
        self.assertEqual(initialize(),{'created':False,'revision':2})
        call_command('migrate',verbosity=0,interactive=False)
        self.client.get('/')
        user.refresh_from_db(); session.refresh_from_db()
        self.assertEqual(user.password,password_hash)
        self.assertTrue(user.check_password('test-only-password'))
        self.assertEqual(session.session_data,'opaque-control')
        self.assertEqual(self.snapshot(),before)

    def test_required_notification_failure_rolls_back_every_write(self):
        self.initial()
        before = self.snapshot()
        def fail_after_insert(change, *, published_at):
            Notification.objects.create(schedule_change=change,group=change.lesson.group,published_at=published_at)
            raise IntegrityError('controlled failure after notification insert')
        with patch('data_import.services.record_schedule_change',side_effect=fail_after_insert):
            with self.assertRaises(IntegrityError): apply(self.r2,expected_revision=1,operator='test')
        self.assertEqual(self.snapshot(),before)
        self.assertEqual((Lesson.objects.get().room,ScheduleChange.objects.count(),Notification.objects.count(),ImportRevision.objects.count()),('204',0,0,1))

    def test_database_constraints_and_immutable_history(self):
        self.updated()
        lesson = Lesson.objects.get()
        for update in [dict(ends_at=lesson.starts_at),dict(status='surprise'),dict(room=None),dict(instructor_status='not_provided')]:
            with self.subTest(update=update), self.assertRaises(IntegrityError), transaction.atomic():
                Lesson.objects.filter(pk=lesson.pk).update(**update)
        for model in [ScheduleChange,ImportRevision,Coverage,Notification]:
            with self.subTest(model=model.__name__), self.assertRaises(IntegrityError), transaction.atomic():
                obj = model.objects.first()
                model.objects.filter(pk=obj.pk).update(id=obj.pk)
        values = {field.attname:getattr(lesson,field.attname) for field in lesson._meta.fields if not field.primary_key}
        values['source_id'] = 'overlap'
        with self.assertRaises(IntegrityError),transaction.atomic(): Lesson.objects.create(**values)
        # Пересечение преподавателя при другой группе тоже запрещено на уровне PostgreSQL.
        other = Group.objects.create(dataset_id=DATASET_ID,source_id='G-OTHER',record_revision=1,source_updated_at=timezone.now(),name='Другая демогруппа')
        values['group_id'] = other.pk
        with self.assertRaises(IntegrityError),transaction.atomic(): Lesson.objects.create(**values)

    def test_import_rejects_active_overlap(self):
        self.initial()
        candidate = payload('r2.json')
        extra = copy.deepcopy(payload('r1.json')['operations'][-1]); extra['record']['source_id'] = 'L-OVERLAP'
        candidate['operations'] = [extra]
        candidate['coverage'][0].update(active_ids=['L-S1-23','L-OVERLAP'],active_count=2)
        before = self.snapshot()
        with self.assertRaisesRegex(ImportRejected,'Пересечение'):
            apply(validate(signed(candidate)),expected_revision=1,operator='test')
        self.assertEqual(self.snapshot(),before)

    def test_read_distinguishes_coverage_freshness_source_and_database_failure(self):
        self.updated()
        for day_number,state,total in [(23,'complete',1),(24,'complete_empty',0),(25,'partial',None),(26,'unknown',None),(27,'unknown',None)]:
            with self.subTest(day=day_number):
                result = self.read(day_number)
                self.assertEqual((result['state'],result['total']),(state,total))
        with patch('studies.selectors.timezone.now',return_value=datetime(2026,12,7,tzinfo=utc.utc)):
            self.assertEqual(self.read()['state'],'expired')
        before = self.snapshot()
        for path,status in [('fixtures/s1/absent.json','source_unavailable'),('fixtures/s1/r1.json','invalid_update')]:
            with self.assertRaises(CommandError):
                call_command('import_dataset',path,expected_revision=2,apply=True,operator='test',stdout=io.StringIO())
            self.assertEqual(self.read()['update_status'],status)
            self.assertEqual(self.snapshot(),before)
        from django.db import OperationalError
        with patch('deanery.views.connection.cursor',side_effect=OperationalError('secret DB exception')):
            response = self.client.get('/health/ready/')
        self.assertEqual(response.status_code,503)
        self.assertContains(response,'Хранилище недоступно',status_code=503)
        self.assertNotContains(response,'secret DB exception',status_code=503)
        self.assertNotContains(response,'L-S1-23',status_code=503)

    def test_two_connections_compete_on_same_head(self):
        self.initial()
        candidate = payload('r2.json')
        candidate['operations'][0]['after']['room'] = '319'
        candidate['operations'][0]['event_id'] = 'EV-COMPETITOR'
        competitor = validate(signed(candidate))
        held, contending, release = threading.Event(),threading.Event(),threading.Event()
        pids, results = {}, {}
        real_get_head = services.get_head
        def guarded(*,lock=False):
            name = threading.current_thread().name
            if name == 'writer-b': contending.set()
            head = real_get_head(lock=lock)
            if name == 'writer-a':
                held.set()
                if not release.wait(8): raise RuntimeError('Synchronization timeout')
            return head
        def worker(name,batch):
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()'); pids[name] = cursor.fetchone()[0]
                results[name] = apply(batch,expected_revision=1,operator=name)['outcome']
            except ImportRejected as exc: results[name] = exc.code
            except Exception as exc: results[name] = repr(exc)
            finally: connections['default'].close()
        with patch('data_import.services.get_head',side_effect=guarded):
            a = threading.Thread(target=worker,args=('writer-a',self.r2),name='writer-a')
            b = threading.Thread(target=worker,args=('writer-b',competitor),name='writer-b')
            a.start()
            try:
                self.assertTrue(held.wait(5)); b.start(); self.assertTrue(contending.wait(5))
                blocked = False
                deadline = time.monotonic()+3
                while time.monotonic()<deadline:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_blocking_pids(%s)',[pids['writer-b']]); blockers=cursor.fetchone()[0]
                    if pids['writer-a'] in blockers: blocked=True; break
                    time.sleep(.01)
                self.assertTrue(blocked,'Вторая реальная связь должна ждать блокировку первой')
            finally:
                release.set(); a.join(8)
                if b.ident: b.join(8)
        self.assertEqual(results,{'writer-a':'applied','writer-b':'head_conflict'})
        self.assertNotEqual(pids['writer-a'],pids['writer-b'])
        self.assertEqual((Dataset.objects.get().current_revision,Lesson.objects.get().room),(2,'318'))
        self.assertEqual(list(ScheduleChange.objects.values_list('event_id',flat=True)),['EV-S1-MOVE'])
        self.assertEqual(Notification.objects.count(),1)

    def test_composite_read_keeps_one_snapshot_during_commit(self):
        self.initial()
        head_read, proceed = threading.Event(),threading.Event()
        result = {}
        def reader():
            def pause(execute,sql,params,many,context):
                value = execute(sql,params,many,context)
                if sql.startswith('SELECT') and 'FROM "data_import_dataset"' in sql:
                    head_read.set()
                    if not proceed.wait(8): raise RuntimeError('reader timeout')
                return value
            try:
                with connections['default'].execute_wrapper(pause): result.update(self.read())
            except Exception as exc: result['error']=repr(exc)
            finally: connections['default'].close()
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            self.assertTrue(head_read.wait(5))
            apply(self.r2,expected_revision=1,operator='writer')
        finally:
            proceed.set(); thread.join(8)
        self.assertNotIn('error',result)
        self.assertEqual((result['revision'],result['lessons'][0]['room'],result['history'],result['notification_events']),(1,'204',[],[]))
        with self.assertNumQueries(0): json.dumps(result)
        current = self.read()
        self.assertEqual((current['revision'],current['lessons'][0]['room'],current['notification_events']),(2,'318',['EV-S1-MOVE']))

    def test_source_failure_and_invalid_update_before_first_import(self):
        for path,status in [('fixtures/s1/absent.json','source_unavailable'),('fixtures/s1/r2.json','invalid_update')]:
            with self.assertRaises(CommandError):
                call_command('import_dataset',path,expected_revision=0,apply=True,operator='test',stdout=io.StringIO())
            result = self.read()
            self.assertEqual((result['state'],result['update_status'],result['total'],result['source_as_of']),('not_prepared',status,None,None))

    def test_valid_two_lesson_slot_swap_is_atomic(self):
        initial = payload('r1.json')
        extra = copy.deepcopy(initial['operations'][-1])
        extra['record'].update(source_id='L-S1-SECOND',starts_at='2026-11-23T10:40:00+03:00',ends_at='2026-11-23T12:10:00+03:00')
        initial['operations'].append(extra)
        initial['coverage'][0].update(active_ids=['L-S1-23','L-S1-SECOND'],active_count=2)
        apply(validate(signed(initial)),expected_revision=0,operator='test')
        update = payload('r2.json')
        second_before = copy.deepcopy(extra['record'])
        second_after = dict(second_before,starts_at='2026-11-23T09:00:00+03:00',ends_at='2026-11-23T10:30:00+03:00',
                            record_revision=2,source_updated_at=update['source_as_of'])
        update['operations'].append(dict(operation='change',entity_type='lesson',expected_record_revision=1,event_id='EV-SWAP-SECOND',
                                          change_type='move',before=second_before,after=second_after,reason='Контроль обмена слотами'))
        update['coverage'] = initial['coverage']
        batch = validate(signed(update))
        self.assertEqual(len(preview(batch,expected_revision=1)['changes']),2)
        apply(batch,expected_revision=1,operator='test')
        self.assertEqual(Lesson.objects.get(source_id='L-S1-23').starts_at,datetime(2026,11,23,7,40,tzinfo=utc.utc))
        self.assertEqual(Lesson.objects.get(source_id='L-S1-SECOND').starts_at,datetime(2026,11,23,6,0,tzinfo=utc.utc))
        self.assertEqual((ScheduleChange.objects.count(),Notification.objects.count(),Dataset.objects.get().current_revision),(2,2,2))
