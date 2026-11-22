from django.urls import path

from . import views

app_name = "maintenance"
urlpatterns = [
    path("", views.MaintenanceListView.as_view(), name="list"),
    path("report/<uuid:asset_pk>/", views.ReportDefectView.as_view(), name="report"),
    path("records/<uuid:pk>/", views.RecordView.as_view(), name="record"),
    path("dispositions/propose/<uuid:asset_pk>/", views.ProposeDispositionView.as_view(), name="propose"),
    path("dispositions/<uuid:pk>/", views.DispositionView.as_view(), name="disposition"),
]
