from django.urls import path
from . import views

urlpatterns=[path('journeys/',views.home,name='journeys'),
    path('journeys/actions/',views.action),
    path('journeys/<int:journey_id>/',views.home),
    path('journeys/<int:journey_id>/state/',views.state),
    path('journeys/<int:journey_id>/actions/',views.action)]
