from django.urls import path
from . import views

urlpatterns = [
    path('search/', views.search_page, name='search'),
    path('search/results/', views.search_results, name='search_results'),
]
