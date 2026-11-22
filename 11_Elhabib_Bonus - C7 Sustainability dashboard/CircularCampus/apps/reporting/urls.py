from django.urls import path

from . import views

app_name = "reporting"
urlpatterns = [
    path("sustainability/", views.SustainabilityView.as_view(), name="sustainability"),
    path("audit/", views.AuditView.as_view(), name="audit"),
]
