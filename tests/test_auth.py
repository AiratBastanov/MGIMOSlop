"""S2: PostgreSQL, настоящие CSRF/Axes и управляемые гонки отдельных соединений."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.db import connection, connections, transaction, IntegrityError, OperationalError
from django.test import Client, RequestFactory, TransactionTestCase
from django.utils import timezone
from accounts import access
from accounts.access import AccessDenied, current_actor, logout_session, set_active
from accounts.models import AuthorizationGuard, Identity, SessionContext, StaffScope
from accounts.provisioning import provision
from accounts.selectors import private_snapshot
from data_import.contracts import validate, ImportRejected
from data_import.provider import SyntheticDatasetProvider
from data_import.services import initialize, apply, check_integrity, get_head
from data_import.state import records, canonical_state
from notifications.models import Notification, NotificationRead
from notifications.actions import mark_read
from studies.models import StudentProfile, ProfileChange
from axes.models import AccessAttempt
from tests.test_import import signed

ROOT = Path(__file__).resolve().parents[1]

class AuthTests(TransactionTestCase):
    def setUp(self):
        initialize()
        for i in range(1,5):
            batch = SyntheticDatasetProvider().load(ROOT / f'fixtures/s{1 if i<3 else 2}/r{i}.json')
            apply(batch,expected_revision=i-1,operator='s2-test')
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.local')
        self.addCleanup(self.temp.cleanup)
        self.credentials_path = Path(self.temp.name) / 'demo.json'
        provision(self.credentials_path)
        self.credentials = json.loads(self.credentials_path.read_text(encoding='utf-8'))
        self.a = Client(enforce_csrf_checks=True)
        self.b = Client(enforce_csrf_checks=True)
        self.notification = Notification.objects.get(schedule_change__event_id='EV-S2-ROOM')
        self.read_url = f'/notifications/{self.notification.pk}/read/'

    def login(self, client=None, username='student_a', **extra):
        client = client or self.a
        client.get('/login/')
        response = client.post('/login/',{'username':username,'password':self.credentials[username],
            'csrfmiddlewaretoken':client.cookies['csrftoken'].value,**extra})
        self.assertEqual(response.status_code,302,response.content[:300])
        return response

    def page(self,client=None,path='/profile/'):
        response = (client or self.a).get(path)
        self.assertEqual(response.status_code,200,response.content[:300])
        return response

    def marker(self,client=None): return self.page(client).context['marker']

    def post(self,url,client=None,**data):
        client = client or self.a
        return client.post(url,{'csrfmiddlewaretoken':client.cookies['csrftoken'].value,**data})

    def request(self,client=None):
        client = client or self.a
        req = RequestFactory().post(self.read_url)
        req.session = SessionStore(session_key=client.cookies['sessionid'].value)
        dict(req.session.items())  # смоделировать уже начатый запрос до lock
        return req

    def scope_change(self):
        payload = json.loads((ROOT/'fixtures/s2/r4.json').read_text(encoding='utf-8'))
        source = timezone.now().isoformat()
        before = records()['profile']['SP-A']
        after = dict(before,record_revision=before['record_revision']+1,group_id='G-MB22',source_updated_at=source)
        payload.update(dataset_revision=5,parent_revision=4,source_as_of=source,operations=[
            {'operation':'create','entity_type':'group','record':dict(source_id='G-MB22',record_revision=1,source_updated_at=source,name='МБ-22',programme_id='PR-MB')},
            {'operation':'change','entity_type':'profile','expected_record_revision':before['record_revision'],
             'event_id':'EV-S2-SCOPE','change_type':'correction','before':before,'after':after,'reason':'Тестовая коррекция группы'}])
        return validate(signed(payload))

    def test_three_logins_profiles_post_logout_and_no_admin(self):
        self.assertEqual(self.a.get('/profile/').status_code,302)
        for username,name,path in [('student_a','Алина Миронова','/profile/'),('student_b','Тимур Светлов','/profile/'),('staff_demo','Елена Рябинина (демо)','/staff/')]:
            c = Client(enforce_csrf_checks=True)
            self.login(c,username)
            page = self.page(c,path)
            self.assertContains(page,name)
            self.assertIn('no-store',page['Cache-Control'])
            self.assertEqual(c.get('/logout/').status_code,405)
            self.assertEqual(self.post('/logout/',c,context=page.context['marker']).status_code,302)
            self.assertEqual(c.get(path).status_code,302)
        self.assertFalse(get_user_model().objects.filter(is_staff=True).exists())
        self.assertEqual(self.a.get('/admin/').status_code,404)

    def test_csrf_rotation_and_safe_redirects(self):
        self.a.get('/login/')
        self.assertEqual(self.a.post('/login/',{'username':'student_a','password':self.credentials['student_a']}).status_code,403)
        anonymous = self.a.session
        anonymous['test'] = 'rotation'; anonymous.save()
        oldkey = anonymous.session_key
        response = self.login(next='https://evil.example/')
        self.assertEqual(response.url,'/today/')
        self.assertNotEqual(self.a.cookies['sessionid'].value,oldkey)
        self.assertEqual(self.a.post(self.read_url,{'context':self.marker()}).status_code,403)
        self.assertEqual(self.a.post('/logout/',{'context':self.marker()}).status_code,403)
        from accounts.views import safe_next
        for value in ['//evil.example/','/\\evil.example/','https://localhost/','/staff/','/profile/?owner=2']:
            self.assertEqual(safe_next(value,'student'),'/today/')

    def test_neutral_inactive_invalid_missing_and_conflicting_identity(self):
        inactive = get_user_model().objects.get(username='student_a')
        set_active(inactive.pk,False,operator='test')
        messages = []
        for username,password in [('student_a',self.credentials['student_a']),('absent','wrong'),('student_b','wrong')]:
            c = Client(enforce_csrf_checks=True); c.get('/login/')
            r = self.post('/login/',c,username=username,password=password)
            self.assertEqual(r.status_code,200); messages.append(r.context['error'])
        self.assertEqual(len(set(messages)),1)
        user = get_user_model().objects.get(username='student_b'); user.is_staff=True; user.save()
        self.a.get('/login/')
        self.assertEqual(self.post('/login/',username='student_b',password=self.credentials['student_b']).status_code,200)

    def test_scope_lookup_count_forgery_staff_function_and_linked_target(self):
        self.login(); self.login(self.b,'student_b')
        a = self.page(path='/notifications/'); b = self.page(self.b,'/notifications/')
        self.assertEqual((a.context['unread'],b.context['unread']),(1,0))
        foreign = StudentProfile.objects.get(source_id='SP-B')
        self.assertEqual(self.a.get(f'/profile/{foreign.pk}/').status_code,404)
        self.assertEqual(self.a.get('/staff/').status_code,403)
        self.assertEqual(self.b.get(f'/notifications/{self.notification.pk}/').status_code,404)
        self.assertEqual(self.post(self.read_url,self.b,context=b.context['marker'],owner_id=a.context['name'],role='staff',group='G-MB21').status_code,404)
        response = self.post(self.read_url,context=a.context['marker'],owner_id=foreign.pk,student_id=foreign.pk,role='staff',is_staff='1')
        self.assertEqual(response.status_code,302)
        self.assertEqual(NotificationRead.objects.get().user.username,'student_a')
        old = Notification.objects.get(schedule_change__event_id='EV-S1-MOVE')
        self.assertEqual(self.a.get(f'/notifications/{old.pk}/').status_code,404)
        staff = Client(enforce_csrf_checks=True); self.login(staff,'staff_demo')
        self.assertEqual(staff.get('/notifications/').status_code,200)
        staff_profile = staff.get('/profile/')
        self.assertEqual(staff_profile.status_code,200)
        self.assertNotContains(staff_profile,'Мои результаты')
        self.assertNotContains(staff_profile,'Алина Миронова')

    def test_read_idempotency_and_replay_provision_preserve_user_state(self):
        self.login(); marker=self.marker()
        self.a.get(f'/notifications/{self.notification.pk}/')
        self.assertEqual(NotificationRead.objects.count(),0)
        for _ in range(2): self.assertEqual(self.post(self.read_url,context=marker).status_code,302)
        before=list(NotificationRead.objects.values()); password=list(get_user_model().objects.values_list('password',flat=True))
        revision=AuthorizationGuard.objects.get().revision; key=self.a.cookies['sessionid'].value
        batch=SyntheticDatasetProvider().load(ROOT/'fixtures/s2/r4.json')
        self.assertEqual(apply(batch,expected_revision=4,operator='test')['outcome'],'noop')
        self.assertEqual(provision(self.credentials_path),[])
        self.assertEqual(list(NotificationRead.objects.values()),before)
        self.assertEqual(list(get_user_model().objects.values_list('password',flat=True)),password)
        self.assertEqual(AuthorizationGuard.objects.get().revision,revision)
        self.assertTrue(Session.objects.filter(session_key=key).exists())
        self.post('/logout/',context=marker); self.login()
        self.assertEqual(self.page(path='/notifications/').context['unread'],0)
        self.assertEqual(list(NotificationRead.objects.values()),before)

    def test_absolute_deadline_at_equality_and_session_save(self):
        start=timezone.now(); self.login()
        context=SessionContext.objects.get(); marker=self.marker()
        self.assertEqual(context.expires_at-context.authenticated_at,timedelta(hours=2))
        near=context.expires_at-timedelta(microseconds=1)
        with patch('django.utils.timezone.now',return_value=near):
            self.assertEqual(self.a.get('/profile/').status_code,200)
            session=self.a.session; session['activity']='save'; session.save()
            self.assertEqual(Session.objects.get(session_key=session.session_key).expire_date,context.expires_at)
            self.assertEqual(self.a.get('/context/',HTTP_X_PAGE_CONTEXT=marker).status_code,200)
        with patch('django.utils.timezone.now',return_value=context.expires_at):
            self.assertEqual(self.a.get('/profile/').status_code,302)
            self.assertEqual(self.post(self.read_url,context=marker).status_code,302)
        self.assertEqual(NotificationRead.objects.count(),0)
        context.refresh_from_db(); self.assertEqual(context.expires_at-context.authenticated_at,timedelta(hours=2))
        with self.assertRaises(IntegrityError),transaction.atomic():
            SessionContext.objects.filter(pk=context.pk).update(expires_at=context.expires_at+timedelta(seconds=1))

    def test_preloaded_request_expiry_rechecked_inside_write(self):
        self.login();marker=self.marker();req=self.request();deadline=SessionContext.objects.get().expires_at
        with patch('django.utils.timezone.now',return_value=deadline),self.assertRaisesRegex(AccessDenied,'session_expired'):
            mark_read(req,self.notification.pk,marker)
        self.assertEqual(NotificationRead.objects.count(),0)

    def test_deadline_crossing_during_target_lookup_denies_write(self):
        self.login();marker=self.marker();req=self.request();deadline=SessionContext.objects.get().expires_at
        with patch('accounts.access.timezone.now',side_effect=[deadline-timedelta(microseconds=1),deadline]),self.assertRaisesRegex(AccessDenied,'session_expired'):
            mark_read(req,self.notification.pk,marker)
        self.assertEqual(NotificationRead.objects.count(),0)

    def test_notification_audience_does_not_override_linked_target(self):
        from studies.models import ScheduleChange
        self.login()
        old=ScheduleChange.objects.get(event_id='EV-S1-MOVE')
        extra=ScheduleChange.objects.create(dataset=old.dataset,revision=old.revision,event_id='TEST-MISMATCH',lesson=old.lesson,
            change_type=old.change_type,before=old.before,after=old.after,reason='Test-only mismatch',operator='test',source_updated_at=old.source_updated_at,recorded_at=timezone.now())
        notification=Notification.objects.create(schedule_change=extra,group_id=StudentProfile.objects.get(source_id='SP-A').group_id,published_at=timezone.now())
        self.assertEqual(self.a.get(f'/notifications/{notification.pk}/').status_code,404)
        self.assertEqual(self.post(f'/notifications/{notification.pk}/read/',context=self.marker()).status_code,404)
        self.assertEqual(NotificationRead.objects.count(),0)
        self.assertEqual(self.page(path='/notifications/').context['unread'],1)

    def test_stale_markers_a_b_a_and_same_user_with_fresh_csrf_current_target(self):
        self.login(); old=self.marker(); oldkey=self.a.cookies['sessionid'].value
        self.post('/logout/',context=old); self.login(username='student_b')
        bmarker=self.marker()
        self.assertEqual(self.post('/logout/',context=old).status_code,409)
        self.post('/logout/',context=bmarker); self.login()
        self.assertNotEqual(self.a.cookies['sessionid'].value,oldkey)
        self.assertEqual(self.post(self.read_url,context=old).status_code,409)
        new=self.marker(); self.post('/logout/',context=new); self.login()
        self.assertEqual(self.post(self.read_url,context=new).status_code,409)
        self.assertEqual(NotificationRead.objects.count(),0)
        self.assertEqual(self.post(self.read_url,context=self.marker()).status_code,302)

    def test_login_after_revocation_binds_final_rotated_session_key(self):
        self.login(); oldkey=self.a.cookies['sessionid'].value
        set_active(get_user_model().objects.get(username='student_a').pk,False,operator='test')
        self.login(username='student_b')
        self.assertNotEqual(self.a.cookies['sessionid'].value,oldkey)
        self.assertContains(self.page(),'Тимур Светлов')
        actor=current_actor(self.request())
        context=SessionContext.objects.get(pk=actor.context_id)
        self.assertEqual(context.session_digest,access.session_digest(self.a.cookies['sessionid'].value))

    def test_binding_is_immutable_and_provision_conflict_is_explicit(self):
        identity=Identity.objects.get(user__username='student_a')
        with self.assertRaises(IntegrityError),transaction.atomic():
            Identity.objects.filter(pk=identity.pk).update(profile=StudentProfile.objects.get(source_id='SP-B'))
        user=get_user_model().objects.get(username='student_a');user.is_staff=True;user.save()
        before=user.password
        with self.assertRaises(ValueError): provision(self.credentials_path)
        user.refresh_from_db();self.assertEqual(user.password,before);self.assertTrue(user.is_staff)

    def test_unrelated_login_logout_does_not_advance_guard_or_invalidate_form(self):
        self.login(); marker=self.marker(); revision=AuthorizationGuard.objects.get().revision
        self.login(self.b,'student_b'); other=self.marker(self.b)
        self.post('/logout/',self.b,context=other)
        self.assertEqual(AuthorizationGuard.objects.get().revision,revision)
        self.assertEqual(self.post(self.read_url,context=marker).status_code,302)

    def test_scope_import_retains_read_but_revokes_access_and_is_atomic(self):
        self.login(); marker=self.marker(); self.post(self.read_url,context=marker)
        binding=list(Identity.objects.values()); sessions=list(SessionContext.objects.values()); revision=AuthorizationGuard.objects.get().revision
        batch=self.scope_change(); apply(batch,expected_revision=4,operator='test')
        self.assertEqual(AuthorizationGuard.objects.get().revision,revision+1)
        self.assertEqual(StudentProfile.objects.get(source_id='SP-A').scope_revision,2)
        self.assertEqual(ProfileChange.objects.count(),1)
        self.assertEqual(list(Identity.objects.values()),binding)
        self.assertEqual(list(SessionContext.objects.values()),sessions)
        self.assertEqual(self.post(self.read_url,context=marker).status_code,409)
        page=self.page(path='/notifications/')
        self.assertEqual(page.context['unread'],0)
        self.assertEqual(self.a.get(f'/notifications/{self.notification.pk}/').status_code,404)
        self.assertEqual(NotificationRead.objects.count(),1)
        self.assertEqual(self.post(self.read_url,context=page.context['marker']).status_code,404)
        check_integrity(get_head())

    def test_invalid_import_and_failed_scope_event_rollback(self):
        batch=self.scope_change(); before=canonical_state(); revision=AuthorizationGuard.objects.get().revision
        for field,value in [('user_id',1),('role','staff'),('password','forbidden')]:
            payload=deepcopy(batch.payload); payload['operations'][1]['after'][field]=value
            with self.assertRaises(ImportRejected): validate(signed(payload))
        with patch('data_import.services.ProfileChange.objects.create',side_effect=IntegrityError('test')):
            with self.assertRaises(IntegrityError): apply(batch,expected_revision=4,operator='test')
        self.assertEqual(canonical_state(),before)
        self.assertEqual(AuthorizationGuard.objects.get().revision,revision)
        self.assertEqual(StudentProfile.objects.get(source_id='SP-A').scope_revision,1)

    def test_db_failure_is_unavailable_not_empty_success(self):
        self.login()
        with patch('accounts.views.current_actor',side_effect=OperationalError('secret')):
            response=self.a.get('/context/',HTTP_X_PAGE_CONTEXT=self.marker())
        self.assertEqual(response.status_code,503)
        self.assertNotIn(b'secret',response.content)

    def race(self,other,first,context_read=False):
        self.login(); marker=self.marker(); req1=self.request(); req2=self.request()
        batch=self.scope_change()
        held,release=threading.Event(),threading.Event()
        pids,results={},{}
        def work(name):
            def barrier(execute,sql,params,many,context):
                value=execute(sql,params,many,context)
                if name==first and 'accounts_authorizationguard' in sql and 'FOR UPDATE' in sql:
                    held.set()
                    if not release.wait(8): raise RuntimeError('barrier timeout')
                return value
            try:
                with connections['default'].cursor() as cursor:
                    cursor.execute('SELECT pg_backend_pid()'); pids[name]=cursor.fetchone()[0]
                with connections['default'].execute_wrapper(barrier):
                    if name=='read':
                        if context_read:
                            from accounts.views import context
                            req1.method='GET'; req1.path=req1.path_info='/context/'; req1.META['HTTP_X_PAGE_CONTEXT']=marker
                            response=context(req1)
                            if response.status_code != 200:
                                results[name]=json.loads(response.content)['state']; return
                        else: mark_read(req1,self.notification.pk,marker)
                    elif other=='logout': logout_session(req2,marker)
                    elif other=='revoke': set_active(get_user_model().objects.get(username='student_a').pk,False,operator='test')
                    else: apply(batch,expected_revision=4,operator='test')
                results[name]='ok'
            except AccessDenied as exc: results[name]=exc.code
            except Exception as exc: results[name]=type(exc).__name__+':'+str(exc)
            finally: connections['default'].close()
        second='other' if first=='read' else 'read'
        a=threading.Thread(target=work,args=(first,)); b=threading.Thread(target=work,args=(second,))
        a.start()
        try:
            self.assertTrue(held.wait(5)); b.start(); blocked=False; deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if second in pids:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_blocking_pids(%s)',[pids[second]])
                        if pids[first] in cursor.fetchone()[0]: blocked=True;break
                time.sleep(.01)
            self.assertTrue(blocked,'Второе соединение действительно должно ждать guard')
        finally:
            release.set(); a.join(8)
            if b.ident: b.join(8)
        self.assertNotEqual(pids['read'],pids['other'])
        self.assertEqual(results['other'],'ok',results)
        self.assertEqual(results['read'],'ok' if first=='read' else ('stale_context' if other=='import' else 'session_expired'),results)
        self.assertEqual(NotificationRead.objects.count(),1 if first=='read' and not context_read else 0)
        if other=='import':
            self.assertEqual(StudentProfile.objects.get(source_id='SP-A').group.source_id,'G-MB22')
            self.assertEqual(ProfileChange.objects.count(),1)

    def test_read_wins_logout(self): self.race('logout','read')
    def test_logout_wins_read(self): self.race('logout','other')
    def test_read_wins_scope_import(self): self.race('import','read')
    def test_scope_import_wins_read(self): self.race('import','other')
    def test_revocation_wins_read(self): self.race('revoke','other')
    def test_read_wins_revocation(self): self.race('revoke','read')
    def test_context_check_wins_logout(self): self.race('logout','read',True)
    def test_logout_wins_context_check(self): self.race('logout','other',True)

    def test_snapshot_during_scope_commit_is_not_published(self):
        self.login(); req=self.request(); batch=self.scope_change()
        held,release=threading.Event(),threading.Event(); result={}
        def reader():
            def barrier(execute,sql,params,many,context):
                value=execute(sql,params,many,context)
                if 'FROM "notifications_notification"' in sql:
                    held.set()
                    if not release.wait(8): raise RuntimeError('timeout')
                return value
            try:
                with connections['default'].execute_wrapper(barrier): result['data']=private_snapshot(req,section='notifications')
            except AccessDenied as exc: result['denied']=exc.code
            finally: connections['default'].close()
        thread=threading.Thread(target=reader); thread.start()
        try:
            self.assertTrue(held.wait(5)); apply(batch,expected_revision=4,operator='test')
        finally: release.set(); thread.join(8)
        self.assertEqual(result,{'denied':'stale_context'})

    def test_revoked_session_cannot_be_saved_by_late_middleware(self):
        from accounts.middleware import GuardedSessionMiddleware
        from django.http import HttpResponse
        self.login(); marker=self.marker(); req=self.request(); key=req.session.session_key
        req.session['late']='save'
        logout_session(self.request(),marker)
        response=GuardedSessionMiddleware(lambda r:HttpResponse()).process_response(req,HttpResponse('success'))
        self.assertEqual(response.status_code,409)
        self.assertNotIn('sessionid',response.cookies)
        self.assertFalse(Session.objects.filter(session_key=key).exists())

    def test_axes_threshold_pair_proxy_ua_cooldown_success_reset(self):
        client=Client(enforce_csrf_checks=True); client.get('/login/')
        start=timezone.now()
        def attempt(username='student_a',password='wrong',ip='127.0.0.1',ua='one',forward='1.2.3.4'):
            return client.post('/login/',{'username':username,'password':password,'csrfmiddlewaretoken':client.cookies['csrftoken'].value},
                REMOTE_ADDR=ip,HTTP_USER_AGENT=ua,HTTP_X_FORWARDED_FOR=forward)
        with patch('django.utils.timezone.now',return_value=start), patch('axes.handlers.proxy.now',return_value=start):
            for i in range(4): self.assertEqual(attempt(ua='one' if i%2 else 'two').status_code,200)
            self.assertEqual(attempt().status_code,429)
        rows=list(AccessAttempt.objects.values('attempt_time','failures_since_start'))
        with patch('django.utils.timezone.now',return_value=start+timedelta(minutes=14)), patch('axes.handlers.proxy.now',return_value=start+timedelta(minutes=14)):
            self.assertEqual(attempt(password=self.credentials['student_a'],ua='new',forward='8.8.8.8').status_code,429)
        self.assertEqual(list(AccessAttempt.objects.values('attempt_time','failures_since_start')),rows)
        # Different pair remains available, arbitrary forwarded IP never changes actual pair.
        self.assertEqual(attempt(username='student_b').status_code,200)
        self.assertEqual(attempt(ip='127.0.0.2').status_code,200)
        self.assertEqual(set(AccessAttempt.objects.values_list('ip_address',flat=True)),{'127.0.0.1','127.0.0.2'})
        with patch('django.utils.timezone.now',return_value=start+timedelta(minutes=15)), patch('axes.handlers.proxy.now',return_value=start+timedelta(minutes=15)):
            # Axes 9 get_failures uses >=; equality is still blocked on this check.
            self.assertEqual(attempt(password=self.credentials['student_a']).status_code,429)
        with patch('django.utils.timezone.now',return_value=start+timedelta(minutes=15,microseconds=1)), patch('axes.handlers.proxy.now',return_value=start+timedelta(minutes=15,microseconds=1)):
            self.assertEqual(attempt(password=self.credentials['student_a']).status_code,302)
        self.assertFalse(AccessAttempt.objects.filter(username='student_a',ip_address='127.0.0.1').exists())

    def test_axes_native_user_agent_rows_age_separately_and_no_csrf_saved(self):
        client=Client(enforce_csrf_checks=True);client.get('/login/');start=timezone.now()
        def attempt(when,ua,password='wrong'):
            with patch('axes.handlers.proxy.now',return_value=when),patch('django.utils.timezone.now',return_value=when):
                return client.post('/login/',{'username':'student_a','password':password,'csrfmiddlewaretoken':client.cookies['csrftoken'].value},HTTP_USER_AGENT=ua)
        for _ in range(2):self.assertEqual(attempt(start,'one').status_code,200)
        for _ in range(2):self.assertEqual(attempt(start+timedelta(seconds=10),'two').status_code,200)
        self.assertEqual(attempt(start+timedelta(seconds=10),'two').status_code,429)
        self.assertEqual(AccessAttempt.objects.count(),2)
        for row in AccessAttempt.objects.all():
            self.assertNotIn(client.cookies['csrftoken'].value,row.post_data)
        self.assertEqual(attempt(start+timedelta(minutes=15,microseconds=1),'two',self.credentials['student_a']).status_code,302)
        self.assertEqual(AccessAttempt.objects.count(),0)
