from django.urls import path

from . import views

app_name = "reporting"
urlpatterns = [
    path("sustainability/", views.SustainabilityView.as_view(), name="sustainability"),
    path("ai-reports/", views.ReportListView.as_view(), name="reports"),
    path("ai-reports/<uuid:pk>/", views.ReportDetailView.as_view(), name="report"),
    path("audit/", views.AuditView.as_view(), name="audit"),
]
