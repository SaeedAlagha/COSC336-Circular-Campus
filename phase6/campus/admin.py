from django.contrib import admin
from .models import Department, Profile, Event
admin.site.site_header='Circular Campus administration'
@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display=['name','active']
@admin.register(Profile)
class ProfileAdmin(admin.ModelAdmin):
    list_display=['user','role','department']
    list_filter=['role','department']
@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display=['created_at','actor','action','reference']
    readonly_fields=['asset','actor','action','reference','detail','created_at']
    def has_add_permission(self,request): return False
    def has_change_permission(self,request,obj=None): return False
    def has_delete_permission(self,request,obj=None): return False
