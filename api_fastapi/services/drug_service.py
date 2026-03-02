import httpx
import logging
from asgiref.sync import sync_to_async
from django.db.models import Q
import asyncio
import re

logger = logging.getLogger(__name__)

class DrugService:
    FDA_BASE_URL = "https://api.fda.gov/drug/label.json"
    FDA_OTC_FILTER = 'openfda.product_type:"HUMAN OTC DRUG"'
    
    # 성분명 매핑 테이블 (FDA generic_name -> KR DUR ingr_eng_name)
    MANUAL_INGR_MAPPING = {
        "DIVALPROEX SODIUM": "VALPROIC ACID",
        "DIVALPROEX": "VALPROIC ACID",
        # 필요 시 추가
    }

    @classmethod
    async def search_fda(cls, name: str):
        """
        특정 제품명으로 FDA 정보 검색 (비동기)
        상세 정보(적응증, 경고, 용법)를 포함하여 반환
        """
        params = {
            'search': f'(openfda.brand_name:"{name}"+OR+openfda.generic_name:"{name}")+AND+{cls.FDA_OTC_FILTER}',
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(cls.FDA_BASE_URL, params=params)
                if res.status_code != 200:
                    return None
                
                data = res.json().get('results', [])
                if not data:
                    return None
                
                result = data[0]
                openfda = result.get('openfda', {})
                
                # 성분명 추출 (generic_name, substance_name 모두 포함)
                # substance_name이 DUR DB의 'Active Moiety'와 일치할 확률이 높음 (예: DIVALPROEX -> VALPROIC ACID)
                generic_names = openfda.get('generic_name', [])
                substance_names = openfda.get('substance_name', [])
                
                combined_ingrs = list(set(generic_names + substance_names))
                
                if not combined_ingrs:
                    combined_ingrs = result.get('active_ingredient', [])
                
                ingr_text = ", ".join(combined_ingrs) if isinstance(combined_ingrs, list) else str(combined_ingrs)

                return {
                    "brand_name": name,
                    "active_ingredients": ingr_text or "Ingredient Not Found",
                    "ingredients": ingr_text, # 호환성을 위해 유지
                    "indications": result.get('indications_and_usage', ["Indications not provided"])[0],
                    "warnings": result.get('warnings', ["Warnings not provided"])[0],
                    "dosage": result.get('dosage_and_administration', ["Dosage info not provided"])[0]
                }
            except Exception as e:
                logger.error(f"Error searching FDA: {e}")
                return None

    @classmethod
    async def get_ingrs_from_fda_by_symptoms(cls, keywords: list):
        """
        영어 증상 키워드로 FDA 관련 성분명 추출 (비동기 + 병렬 처리)
        
        [개선] count=openfda.generic_name.exact 를 사용하여 특정 증상에 대해
        가장 많이 허가된 다양한 활성 성분 TOP 20을 집계합니다.
        기존처럼 search+limit=50을 쓰면 결과가 아세트아미노펜 브랜드 50개로
        꽉 차서 이부프로펜, 나프록센, 아스피린 등이 누락되는 문제가 있었습니다.
        """
        all_ingrs = set()
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            tasks = []
            for kw in keywords:
                # count 엔드포인트: OTC 허가 성분명을 빈도 기준으로 집계
                url = (
                    f'{cls.FDA_BASE_URL}'
                    f'?search=indications_and_usage:"{kw}"'
                    f'+AND+{cls.FDA_OTC_FILTER}'
                    f'&count=openfda.generic_name.exact'
                )
                tasks.append(client.get(url))
            
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            
            for res in responses:
                if isinstance(res, httpx.Response) and res.status_code == 200:
                    try:
                        results = res.json().get('results', [])
                        # count 결과: [{"term": "ACETAMINOPHEN", "count": 911}, ...]
                        # 상위 20개 성분만 추출 (복합제 성분명은 쉼표 구분으로 분리 시도)
                        for item in results[:20]:
                            term = item.get('term', '').upper()
                            if not term:
                                continue
                            # 복합제인 경우 쉼표/AND로 개별 성분 분리
                            parts = re.split(r',\s*| AND ', term)
                            for part in parts:
                                part = part.strip()
                                # 숫자/단위가 포함된 부가 설명 제거 (예: "500 MG")
                                part_clean = re.sub(r'\s+\d+.*$', '', part).strip()
                                if part_clean and len(part_clean) > 2:
                                    all_ingrs.add(part_clean)
                    except Exception as e:
                        logger.warning(f"[FDA count parse error]: {e}")
                        continue
                        
        return list(all_ingrs)

    @staticmethod
    @sync_to_async
    def get_dur_by_ingr(ingr_text):
        """제품 검색 시 성분 텍스트로 한국 DUR 조회"""
        from drugs.models import DurMaster
        if not ingr_text:
            return []
            
        query = Q()
        for i in ingr_text.replace(',', '/').split('/'):
            target = i.strip().lower()
            if len(target) > 1:
                query |= Q(ingr_eng_name__icontains=target)
        
        # 쿼리셋 평가를 위해 list()로 변환
        durs = list(DurMaster.objects.filter(query))
        
        return [{
            "type": d.dur_type,
            "ingr_name": d.ingr_kor_name,
            "warning_msg": d.prohbt_content or d.remark,
            "severity": d.critical_value
        } for d in durs]

    @classmethod
    async def get_fda_warnings_by_ingr(cls, ingr_name: str):
        """
        성분명으로 FDA 경고(Warnings) 정보 조회
        """
        params = {
            'search': f'openfda.generic_name:"{ingr_name}"+AND+{cls.FDA_OTC_FILTER}',
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            try:
                res = await client.get(cls.FDA_BASE_URL, params=params)
                if res.status_code == 200:
                    data = res.json().get('results', [])
                    if data:
                        return data[0].get('warnings', ["No FDA warning found."])[0]
            except Exception as e:
                logger.warning(f"Error fetching FDA warnings for '{ingr_name}': {e}")
        return None

    @classmethod
    async def get_enriched_dur_info(cls, ingr_list: list):
        """
        영어 성분명 리스트를 받아 KR DUR 및 FDA Warning 정보를 병합하여 반환
        """
        from drugs.models import DurMaster
        enriched_data = []

        # 1. 고유 성분명으로 정리
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))

        for ingr in unique_ingrs:
            # 2. KR DUR 조회 (동기 DB 호출을 비동기로 래핑해야 함 - 여기서는 sync_to_async 사용 권장되지만, loop 내 호출이므로 주의)
            # 성능을 위해 전체 쿼리를 먼저 하고 매핑하는 것이 좋지만, 일단 간단 구현
            durs = await cls._get_kr_durs_async(ingr)
            
            # 3. FDA Warning 조회
            fda_warn = await cls.get_fda_warnings_by_ingr(ingr)
            
            # [Translation & Summarization]
            if fda_warn:
                from services.ai_service import AIService
                # 요약 실행 (병목 가능성 있으나, 정확도 위해 수행)
                summary = await AIService.summarize_fda_warning(fda_warn)
                if summary:
                    fda_warn = summary
            
            enriched_data.append({
                "ingredient": ingr,
                "kr_durs": durs,
                "fda_warning": fda_warn
            })
            
        return enriched_data

    @classmethod
    async def _get_kr_durs_async(cls, ingr_name):
        """비동기 문맥에서 DB 호출을 위한 헬퍼 (Robust Search with Lazy LLM)"""
        from drugs.models import DurMaster
        from django.db.models import Q
        
        if not ingr_name: return []
        
        # 1. Cleaning
        target_name = ingr_name.strip().lower()
        if not target_name: return []

        # 2. Synonym Mapping (Common miss-matches)
        SYNONYMS = {
            "acetaminophen": ["acetaminophen", "paracetamol"],
            "paracetamol": ["acetaminophen", "paracetamol"],
            "aspirin": ["aspirin", "acetylsalicylic acid"],
            "ibuprofen": ["ibuprofen"],
            "naproxen": ["naproxen"],
            "diphenhydramine": ["diphenhydramine"],
        }
        
        search_candidates = set()
        search_candidates.add(target_name)
        
        # Add synonyms
        if target_name in SYNONYMS:
            search_candidates.update(SYNONYMS[target_name])
            
        # Add first word
        first_word = target_name.split()[0]
        if len(first_word) > 3:
            search_candidates.add(first_word)

        logger.debug(f"Search candidates for '{ingr_name}': {search_candidates}")

        # 3. Construct Query
        q_obj = Q()
        for cand in search_candidates:
            q_obj |= Q(ingr_eng_name__icontains=cand)
            q_obj |= Q(ingr_kor_name__icontains=cand)

        # Sync code to create queryset is fine
        durs_qs = DurMaster.objects.filter(q_obj).distinct()
        
        # Async execution of DB query
        durs_list = await sync_to_async(list)(durs_qs)
        
        # [Lazy LLM Expansion]
        if not durs_list and len(target_name) > 2:
            from services.ai_service import AIService
            logger.debug(f"No direct DUR match for '{target_name}'. Requesting AI synonyms...")
            
            ai_synonyms = await AIService.get_synonyms(ingr_name)
            logger.debug(f"AI Synonyms for '{ingr_name}': {ai_synonyms}")
            
            if ai_synonyms:
                q_retry = Q()
                for syn in ai_synonyms:
                    q_retry |= Q(ingr_eng_name__icontains=syn)
                    q_retry |= Q(ingr_kor_name__icontains=syn)
                
                durs_retry_qs = DurMaster.objects.filter(q_retry).distinct()
                durs_list = await sync_to_async(list)(durs_retry_qs)



        # [Dedup & Translation]
        DUR_TYPE_KOR_MAP = {
            "PREGNANCY": "임부 금기/주의",
            "COMBINED": "병용 금기",
            "AGE_SPECIFIC": "연령 금기",
            "ELDERLY": "노인 주의",
            "MAX_CAPACITY": "용량 주의",
            "MAX_DURATION": "투여 기간 주의",
            "EFFICACY_DUPLICATE": "효능 중복 주의",
            "DOSAGE_DUPLICATE": "용법 주의",
            "ADMINISTRATION_DUPLICATE": "투여 경로 주의",
            "LACTATION": "수유부 주의",
            "WEIGHT": "체중 주의",
            "KIDNEY": "신장 질환 주의",
            "LIVER": "간 질환 주의",
            "G6PD": "특정 효소 결핍 주의",
            "PEDIATRIC": "소아 주의",
        }

        # Group by type to remove duplicates and combine messages
        grouped_results = {}
        
        for d in durs_list:
            d_type = d.dur_type
            kor_type = DUR_TYPE_KOR_MAP.get(d_type, d_type) # Fallback to original if not mapped
            content = (d.prohbt_content or d.remark or "").strip()
            
            if not content: continue
            
            if kor_type not in grouped_results:
                grouped_results[kor_type] = {
                    "type": kor_type, # Use localized name
                    "original_type": d_type,
                    "kor_name": d.ingr_kor_name,
                    "warnings": set() # Use set for dedup content
                }
            
            grouped_results[kor_type]["warnings"].add(content)

        results = []
        for key, val in grouped_results.items():
            # Combine unique warnings into one string
            combined_warning = "\n".join(sorted(list(val["warnings"])))
            results.append({
                "type": val["type"],
                "kor_name": val["kor_name"],
                "warning": combined_warning
            })
            
        logger.debug(f"Found {len(results)} DUR categories for '{ingr_name}' (after dedup/translation).")
        return results



    @staticmethod
    @sync_to_async
    def search_eyak_drug(keyword: str):
        """
        DrugPermitInfo에서 제품명 또는 업체명으로 약품 검색 (사용자 확인: 데이터 존재함)
        """
        from drugs.models import DrugPermitInfo
        
        # 검색어 공백 제거
        keyword = keyword.strip()

        # 최대 100개까지 반환 (스크롤 고려)
        if keyword:
            results = DrugPermitInfo.objects.filter(
                Q(item_name__icontains=keyword) | 
                Q(entp_name__icontains=keyword)
            )[:100]
        else:
            # 검색어 없으면 상위 100개 반환 (전체 보기)
            results = DrugPermitInfo.objects.all()[:100]

        return [{
            "item_seq": item.item_seq,
            "item_name": item.item_name,
            "entp_name": item.entp_name
        } for item in results]

    @classmethod
    async def get_us_mapping(cls, ingredient_name: str):
        url = f"https://api.fda.gov/drug/label.json?search=openfda.substance_name:\"{ingredient_name}\"+AND+openfda.product_type:\"HUMAN OTC DRUG\"&limit=3"
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            if response.status_code != 200:
                return {"error": "미국 내 해당 성분 의약품을 찾을 수 없습니다."}
            data = response.json()
            return [
                {
                    "brand_name": res.get("openfda", {}).get("brand_name", ["N/A"])[0],
                    "dosage_form": res.get("openfda", {}).get("dosage_form", ["N/A"])[0],
                    "warnings": res.get("warnings", ["N/A"])[0][:200]
                } for res in data.get("results", [])
            ]

    @classmethod
    def compare_dosage_and_warn(cls, fda_active_ingredient_text: str, kr_dosage_mg: float) -> dict:
        """
        FDA의 active_ingredient 텍스트에서 mg 단위를 추출하여 한국 처방량과 비교
        fda_active_ingredient_text 예: "ACETAMINOPHEN 500mg" 또는 "Ibuprofen 200 mg"
        kr_dosage_mg 예: 300.0 (한국 기준 함량)
        """
        warning_msg = None
        us_dosage_mg = None
        
        # 정규식을 이용해 mg 수치 추출 (예: 500 mg, 500.0mg 등)
        match = re.search(r'(\d+(?:\.\d+)?)\s*mg', fda_active_ingredient_text, re.IGNORECASE)
        if match:
            try:
                us_dosage_mg = float(match.group(1))
            except ValueError:
                pass
                
        if us_dosage_mg is not None and kr_dosage_mg > 0:
            diff_ratio = us_dosage_mg / kr_dosage_mg
            if diff_ratio >= 1.5:
                warning_msg = f"주의: 미국 제품의 함량({us_dosage_mg}mg)이 한국 기준({kr_dosage_mg}mg)보다 1.5배 이상 높습니다. 복용 전 약사와 상담하세요."
            elif diff_ratio <= 0.5:
                warning_msg = f"주의: 미국 제품의 함량({us_dosage_mg}mg)이 한국 기준({kr_dosage_mg}mg)보다 0.5배 이하로 낮아 권장 효과에 미달할 수 있습니다."
            else:
                warning_msg = f"미국 제품의 함량({us_dosage_mg}mg)은 한국 처방 기준({kr_dosage_mg}mg)과 유사한 수준입니다."
        else:
            warning_msg = "함량(mg) 정보를 명확히 추출하지 못했거나 기준량이 입력되지 않아 비교할 수 없습니다. 제조사 라벨을 반드시 확인하세요."
            
        return {
            "us_dosage_mg": us_dosage_mg,
            "kr_dosage_mg": kr_dosage_mg,
            "warning": warning_msg
        }