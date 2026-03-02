from fastapi import FastAPI
from typing import List
import requests

# Django 모델 연동 (FastAPI에서 Django ORM 사용 설정 필요)
from drugs.models import DurMaster, EYakInfo

app = FastAPI()

class GlobalDrugEngine:
    def __init__(self):
        self.fda_base_url = "https://api.fda.gov/drug/label.json"

    async def get_integrated_info(self, drug_name_en: str):
        """
        1. 미국 FDA 정보를 가져오고
        2. 그 성분으로 한국 DUR 정보를 매핑하여 통합 리포트 생성
        """
        # [Step 1] FDA API 호출 (미국 정보)
        fda_data = self._fetch_fda_info(drug_name_en)
        
        # [Step 2] 성분명 매핑 및 한국 DUR 조회
        # FDA 응답에서 추출한 성분명(예: Acetaminophen)으로 우리 DB 검색
        ingr_en = fda_data.get('active_ingredient', drug_name_en)
        korean_dur = self._get_korean_dur_info(ingr_en)

        return {
            "us_info": fda_data,
            "kr_dur_info": korean_dur,
            "safety_message": self._generate_safety_summary(korean_dur)
        }

    def _get_korean_dur_info(self, ingr_en: str):
        # 우리 DB(DurMaster)에서 영문 성분명으로 모든 금기 정보를 긁어옵니다.
        # 이전에 수집한 ingr_eng_name 컬럼을 활용합니다.
        durs = DurMaster.objects.filter(ingr_eng_name__icontains=ingr_en.lower())
        
        results = []
        for dur in durs:
            results.append({
                "type": dur.dur_type,
                "reason": dur.prohbt_content,
                "critical_value": dur.critical_value
            })
        return results

    def _fetch_fda_info(self, name: str):
        # 실제 FDA Open API 호출 로직
        params = {'search': f'openfda.brand_name:"{name}"', 'limit': 1}
        res = requests.get(self.fda_base_url, params=params)
        # ... (상세 파싱 로직) ...
        return {"active_ingredient": "Acetaminophen", "indications": "Pain relief"}

    def _generate_safety_summary(self, dur_list):
        if not dur_list:
            return "한국 DUR 기준, 특이사항이 발견되지 않았습니다. 현지 가이드를 따르세요."
        return f"주의! 한국 DUR 기준 {len(dur_list)}건의 금기/주의 사항이 있습니다."