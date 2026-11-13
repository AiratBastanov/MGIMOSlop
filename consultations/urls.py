from django.urls import path
from . import views

urlpatterns = [
    path('consultations/', views.page, name='consultations'),
    path('consultations/state/', views.state, name='consultations_state'),
    path('consultations/actions/', views.action, name='consultation_action'),
    path('consultations/bookings/<int:booking_id>/', views.page, name='consultation_booking'),
    path('consultations/bookings/<int:booking_id>/state/', views.state, name='consultation_booking_state'),
]
