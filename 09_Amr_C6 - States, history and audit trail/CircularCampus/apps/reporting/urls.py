from django.urls import path

from . import views

app_name = "reporting"
urlpatterns = [
    path("audit/", views.AuditView.as_view(), name="audit"),
]
