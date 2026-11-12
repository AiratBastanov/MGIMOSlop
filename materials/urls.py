from django.urls import path
from . import views

urlpatterns=[
    path('study/materials/files/<uuid:attachment_id>/',views.download,name='material-draft-download'),
    path('study/materials/revisions/<int:revision_id>/files/<uuid:attachment_id>/',views.download,name='material-revision-download'),
    path('study/materials/',views.student_list,name='materials'),
    path('study/materials/new/',views.new,name='material-new'),
    path('study/materials/editor/',views.editor,name='material-editor-new'),
    path('study/materials/<int:material_id>/editor/',views.editor,name='material-editor'),
    path('study/materials/<int:material_id>/',views.detail,name='material-detail'),
    path('teacher/',views.teacher_workspace,name='teacher-workspace'),
    path('teacher/assignments/<int:assignment_id>/',views.policy,name='submission-policy'),
    path('teacher/materials/<int:material_id>/',views.teacher_detail,name='teacher-material'),
]
