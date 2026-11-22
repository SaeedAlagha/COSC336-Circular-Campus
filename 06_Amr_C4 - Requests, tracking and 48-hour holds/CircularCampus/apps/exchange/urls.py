from django.urls import path

from . import views

app_name = "exchange"
urlpatterns = [
    path("", views.RequestListView.as_view(), name="list"),
    path("new/", views.RequestFormView.as_view(), name="new"),
    path("<uuid:pk>/", views.RequestDetailView.as_view(), name="request"),
    path("<uuid:pk>/edit/", views.RequestFormView.as_view(), name="edit"),
    path("<uuid:pk>/action/", views.RequestActionView.as_view(), name="action"),
]
