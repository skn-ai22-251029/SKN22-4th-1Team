import os
import sys
import django
from django.db import transaction

# 1. Django 환경 설정 (상위 디렉토리의 backend_django 추가)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend_django')))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from drugs.models import EYakInfo, DrugPermitInfo, UnifiedDrugInfo

class UnifiedLoader:
    def process_unification(self):
        print("--- [START] 의약품 통합 정보 생성 (UnifiedDrugInfo) ---")
        
        # 전체 e약은요 데이터 조회
        eyak_items = EYakInfo.objects.all()
        total_count = eyak_items.count()
        print(f"   - 총 {total_count}건의 e약은요 기본 데이터를 처리합니다.")
        
        processed_count = 0
        
        for eyak in eyak_items.iterator(chunk_size=1000):
            processed_count += 1
            
            # 1. 매칭되는 허가정보 검색 (제품명 기준)
            permit_info = DrugPermitInfo.objects.filter(item_name=eyak.item_name).first()
            
            # 2. 통합 데이터 구성
            defaults = {
                'item_name': eyak.item_name,
                'entp_name': eyak.entp_name,
                'efficacy': eyak.efficacy,
                'use_method': eyak.use_method,
                'precautions': eyak.precautions,
                'interaction': eyak.interaction,
                'side_effects': eyak.side_effects,
                'item_image': eyak.item_image,
                # Default None fields
                'etc_otcc_name': None,
                'main_ingr_eng': None,
                'main_ingr_kor': None,
                'source_updated_at': None
            }
            
            # 허가정보가 있으면 덮어쓰기
            if permit_info:
                defaults['etc_otcc_name'] = permit_info.etc_otcc_name
                defaults['main_ingr_eng'] = permit_info.main_ingr_eng
                defaults['main_ingr_kor'] = permit_info.main_ingr_kor
                defaults['source_updated_at'] = permit_info.source_updated_at # 허가일/수정일
            
            # 3. UnifiedDrugInfo 저장/업데이트
            UnifiedDrugInfo.objects.update_or_create(
                item_seq=eyak.item_seq, # PK는 e약은요 기준 (가장 확실한 식별자)
                defaults=defaults
            )
            
            if processed_count % 100 == 0:
                print(f"   ... {processed_count}/{total_count} 건 처리 완료")
        
        print(f"--- [FINISH] 통합 로직 완료. 총 {processed_count}건 처리됨. ---")

if __name__ == "__main__":
    loader = UnifiedLoader()
    loader.process_unification()
