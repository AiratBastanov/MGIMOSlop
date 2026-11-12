from django.urls import path, include
from accounts import views
from student_requests import views as request_views
from deanery.views import status_page
from profiles import views as profile_views
from notifications import views as notification_views
from accounts.support import support

urlpatterns = [
    path('',include('workspaces.urls')),
    path('support/',support,name='support'),
    path('notifications/read-all/',notification_views.recipient_action,{'action':'read-all'},name='notification_read_all'),
    path('notifications/viewed-request/',notification_views.recipient_action,{'action':'viewed-request'},name='notification_viewed_request'),
    path('notifications/trash/',notification_views.recipient_action,{'action':'trash'},name='notification_trash'),
    path('notifications/restore/',notification_views.recipient_action,{'action':'restore'},name='notification_restore'),
    path('requests/<int:request_id>/state/',request_views.request_state,name='request_state'),
    path('', include('materials.urls')),
    path('profile/edit/',profile_views.edit_profile,name='profile_edit'),
    path('profile/group/',profile_views.my_group,name='profile_group'),
    path('profile/verify-email/',profile_views.confirm_email,name='email_confirm'),
    path('profile/verify-email/resend/',profile_views.resend_verification),
    path('people/<int:identity_id>/photo/',profile_views.photo,name='person_photo'),
    path('staff/resources/',profile_views.resources,name='group_resources'),
    path('staff/resources/<int:resource_id>/',profile_views.resources,name='group_resource_edit'),
    path('notifications/clear/',notification_views.clear,name='notification_clear'),
    path('notifications/undo/<uuid:batch_id>/',notification_views.undo,name='notification_undo'),
    path('',views.home,name='home'),
    path('login/',views.login_page,name='login'),
    path('logout/',views.logout_page,name='logout'),
    path('today/',views.today,name='today'),
    path('schedule/',views.schedule,name='schedule'),
    path('study/',views.study,name='study'),
    path('study/enrollments/<int:enrollment_id>/',views.enrollment_detail,name='enrollment_detail'),
    path('assessments/<int:assessment_id>/',views.assessment_detail,name='assessment_detail'),
    path('lessons/<int:lesson_id>/',views.lesson_detail,name='lesson_detail'),
    path('announcements/<int:announcement_id>/',views.announcement_detail,name='announcement_detail'),
    path('profile/',views.profile,name='profile'),
    path('profile/<int:profile_id>/',views.profile),
    path('requests/',request_views.request_list,name='requests'),
    path('requests/new/',request_views.request_new,name='request_new'),
    path('requests/<int:request_id>/',request_views.request_detail,name='request_detail'),
    path('staff/',request_views.staff_queue,name='staff'),
    path('staff/requests/',request_views.staff_queue,name='staff_queue'),
    path('staff/requests/<int:request_id>/',request_views.staff_detail,name='staff_request_detail'),
    path('notifications/',views.notifications,name='notifications'),
    path('notifications/<int:notification_id>/',views.notifications),
    path('notifications/<int:notification_id>/read/',views.read_notification,name='notification_read'),
    path('context/',views.context,name='context'),
    path('health/ready/',status_page,name='health'),
]
handler404 = 'accounts.views.not_found'
handler500 = 'accounts.views.server_error'
