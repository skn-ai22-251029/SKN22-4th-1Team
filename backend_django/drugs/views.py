# backend_django/drugs/views.py
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .models import DrugMaster
from django.db.models import Q

class DrugSearchView(APIView):
    def get(self, request):
        query = request.query_params.get('q', '').strip()
        
        if not query:
            return Response({"error": "검색어가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST)

        # 1. 성분명(영문) 또는 제품명(국문)으로 검색 (LIKE 검색)
        # 2. 대소문자 구분 없이 검색 (
        #    'ilike'는 PostgreSQL 전용이므로, MySQL 호환을 위해 대소문자 구분 없는 비교를 사용하거나
        #    DB 설정에 따라 ilike를 지원하는 확장(pg_trgm)을 사용해야 합니다.
        #    여기서는 일반적인 문자열 검색을 구현합니다.)
        
        # MySQL에서 대소문자 구분 없는 검색을 위해 LOWER() 함수 사용
        queryset = DrugMaster.objects.filter(
            Q(ingredient_en__icontains=query) |
            Q(item_name__icontains=query)
        ).values(
            'ingredient_en', 'ingredient_kr', 'item_name', 'efficacy', 'precautions', 'dur_data'
        )

        return Response({
            "results": list(queryset),
            "count": len(queryset)
        })
