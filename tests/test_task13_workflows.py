"""Focused copies, retired presentation and authorized status representation."""
from django.test import TransactionTestCase, Client
from django.test.utils import CaptureQueriesContext
from django.db import connection
from accounts.access import current_actor
from accounts.support import topics
from student_requests.models import Request, RequestEvent
from notifications.models import Notification, NotificationReceipt
from notifications.recipient_actions import trash_event
from notifications.selectors import inbox_page
from tests import test_auth, test_task10_notifications as helpers


class WorkflowTests(TransactionTestCase):
    setUp=test_auth.AuthTests.setUp;login=test_auth.AuthTests.login;request=test_auth.AuthTests.request;page=test_auth.AuthTests.page;marker=test_auth.AuthTests.marker
    actor=helpers.NotificationWorkflowTests.actor;create=helpers.NotificationWorkflowTests.create;transition=helpers.NotificationWorkflowTests.transition

    def test_copies_4_10_accept_retry_and_forged_rejection_no_events(self):
        self.login()
        for value in ('4','10'):
            form=self.a.get('/requests/new/').context['component_data']
            payload=dict(context=form['marker'],submission_token=form['token'],request_type='study_certificate',purpose='employment',recipient='Синтетическая организация',copies=value,comment='',csrfmiddlewaretoken=self.a.cookies['csrftoken'].value)
            response=self.a.post('/requests/new/',payload,HTTP_X_COMPONENT='1',HTTP_X_REQUEST_FORM='1')
            self.assertEqual(response.status_code,200,response.content)
            before=Request.objects.count();self.assertEqual(self.a.post('/requests/new/',payload,HTTP_X_COMPONENT='1',HTTP_X_REQUEST_FORM='1').status_code,200);self.assertEqual(Request.objects.count(),before)
            self.assertEqual(Request.objects.latest('id').payload_data['copies'],int(value))
        for value in ('','0','11','99','1000000','-1','1.5','1e6','abc','9'*1000):
            form=self.a.get('/requests/new/').context['component_data'];payload.update(context=form['marker'],submission_token=form['token'],copies=value)
            before=(Request.objects.count(),RequestEvent.objects.count(),Notification.objects.count())
            response=self.a.post('/requests/new/',payload,HTTP_X_COMPONENT='1',HTTP_X_REQUEST_FORM='1')
            self.assertEqual(response.status_code,400,(value[:20],response.content[:100]))
            self.assertEqual(before,(Request.objects.count(),RequestEvent.objects.count(),Notification.objects.count()))
        self.assertIn('от 1 до 10',str(topics('student')))

    def test_retired_trash_preserves_dismissal_and_inbox_read_all(self):
        self.login();actor=self.actor();row,event=self.create()
        page=self.a.get('/notifications/').context;item=next(v for v in page['notifications'] if v['id']==event.pk)
        trash_event(self.request(),actor.marker,item['recipient_token'])
        before=list(NotificationReceipt.objects.values())
        for url in ('/notifications/','/notifications/?mode=history'):
            response=self.a.get(url);self.assertEqual(response.status_code,200)
            data=response.context['component_data'];self.assertNotIn('В корзину',data['html']);self.assertNotIn('Восстановить',data['html'])
            self.assertNotIn(f'notification-{event.pk}"',data['html'])
        old=self.a.get('/notifications/?mode=trash');self.assertEqual(old.status_code,302);self.assertEqual(old.url,'/notifications/?mode=history')
        self.assertEqual(before,list(NotificationReceipt.objects.values()))
        self.assertNotIn('Корзин',str(topics('student')))
        fresh=self.a.get('/notifications/').context['component_data']
        response=self.a.post('/notifications/read-all/',dict(context=actor.marker,snapshot=fresh['read_all_token'],csrfmiddlewaretoken=self.a.cookies['csrftoken'].value))
        self.assertEqual(response.status_code,200);self.assertTrue(Notification.objects.filter(pk=event.pk).exists())

    def test_authorized_status_payload_has_current_fragments_and_staff_version(self):
        self.login();row,event=self.create();actor=self.actor()
        response=self.a.get(f'/requests/{row.pk}/state/',HTTP_X_PAGE_CONTEXT=actor.marker,HTTP_X_COMPONENT='1')
        self.assertEqual(response.status_code,200,response.content)
        data=response.json();self.assertIn('id="request-current"',data['html']);self.assertIn('request-history',data['html']);self.assertEqual(data['viewed']['version'],row.version)
        self.login(self.b,'student_b');self.assertEqual(self.b.get(f'/requests/{row.pk}/state/',HTTP_X_PAGE_CONTEXT=self.actor(self.b).marker,HTTP_X_COMPONENT='1').status_code,404)
        staff=Client(enforce_csrf_checks=True);self.login(staff,'staff_demo')
        result=staff.get(f'/requests/{row.pk}/state/',HTTP_X_PAGE_CONTEXT=self.actor(staff).marker,HTTP_X_COMPONENT='1');self.assertEqual(result.status_code,200,result.content)
        self.assertIn('expected_version',result.json()['html']);self.assertIn('no-store',result['Cache-Control'])
