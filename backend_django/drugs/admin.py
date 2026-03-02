from django.contrib import admin
from .models import EYakInfo, DurMaster

@admin.register(EYakInfo)
class EYakInfoAdmin(admin.ModelAdmin):
    list_display = ('item_name', 'entp_name', 'source_updated_at')
    search_fields = ('item_name', 'item_seq')

@admin.register(DurMaster)
class DurMasterAdmin(admin.ModelAdmin):
    list_display = ('dur_type', 'ingr_kor_name', 'critical_value', 'notification_date')
    list_filter = ('dur_type',)
    search_fields = ('ingr_kor_name', 'ingr_eng_name')