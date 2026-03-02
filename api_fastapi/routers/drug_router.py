from fastapi import APIRouter, Query, HTTPException
from services.drug_service import DrugService
from services.map_service import MapService
from typing import List, Dict

router = APIRouter(prefix="/api/drugs", tags=["drugs"])

@router.get("/search")
async def search_drugs(q: str = Query("", min_length=0)):
    """
    Search drugs by name or manufacturer from EYakInfo.
    If q is empty, returns top 100 drugs.
    """
    try:
        results = await DrugService.search_eyak_drug(q)
        return results
    except Exception as e:
        print(f"Error searching drugs: {e}")
        return []

@router.get("/us-roadmap")
async def get_us_roadmap(
    ingredients: List[str] = Query(..., description="복합제 혹은 단일제 주성분 영문명 리스트 (예: ACETAMINOPHEN)"), 
    kr_dosage_mg: float = Query(0.0, description="한국 기존 약물 기준 함량(mg) - 단일제 비교 시 활용")
):
    """
    한국 약품 주성분(들)을 기반으로 미국 가용 OTC 대체재 큐레이션 및 소통 카드 생성 (with Supabase Cache)
    """
    from services.supabase_service import SupabaseService
    
    # 1. Generate unique Cache Key
    # Sort ingredients alphabetically to ensure consistent keys regardless of input order
    sorted_ingrs = sorted([ingr.strip().upper() for ingr in ingredients if ingr.strip()])
    ingrs_str = "_".join(sorted_ingrs)
    cache_key = f"roadmap_{kr_dosage_mg}_{ingrs_str}"
    
    # 2. Check Cache
    try:
        cached_data = await SupabaseService.get_roadmap_cache(cache_key)
        if cached_data:
            return {
                "requested_ingredients": ingredients,
                "mapping_result": cached_data.get("mapping_result", {}),
                "pharmacist_card": cached_data.get("pharmacist_card", {}),
                "dosage_warnings": cached_data.get("dosage_warnings", [])
            }
    except Exception as e:
        print(f"[Roadmap Cache Read Error]: {e}")

    # 3. Cache Miss - Generate new roadmap
    try:
        # 복합제 듀얼 매치 모듈 호출 (Full Match or Component Match)
        mapping_result = await MapService.find_optimal_us_products(ingredients)
        
        # 약사 상담 브릿지 생성
        pharmacist_card = MapService.generate_pharmacist_card(ingredients)
        
        # 용량 경고 분석
        dosage_warnings = []
        if kr_dosage_mg > 0 and mapping_result.get("recommendations"):
            match_type = mapping_result.get("match_type")
            
            if match_type == "FULL_MATCH":
                for rec in mapping_result["recommendations"]:
                    active_ingr = rec.get("active_ingredient", "")
                    warn_info = DrugService.compare_dosage_and_warn(active_ingr, kr_dosage_mg)
                    if warn_info.get("us_dosage_mg") is not None:
                        dosage_warnings.append({
                            "brand_name": rec.get("brand_name"),
                            "warning_info": warn_info
                        })
            elif match_type == "COMPONENT_MATCH":
                first_ingr_recs = mapping_result["recommendations"][0].get("products", [])
                for rec in first_ingr_recs[:3]: 
                    active_ingr = rec.get("active_ingredient", "")
                    warn_info = DrugService.compare_dosage_and_warn(active_ingr, kr_dosage_mg)
                    if warn_info.get("us_dosage_mg") is not None:
                        dosage_warnings.append({
                            "brand_name": rec.get("brand_name"),
                            "warning_info": warn_info
                        })

        # 4. Save to Cache asynchronously
        import asyncio
        asyncio.create_task(
            SupabaseService.set_roadmap_cache(
                query_text=cache_key,
                mapping_result=mapping_result,
                pharmacist_card=pharmacist_card,
                dosage_warnings=dosage_warnings
            )
        )

        return {
            "requested_ingredients": ingredients,
            "mapping_result": mapping_result,
            "pharmacist_card": pharmacist_card,
            "dosage_warnings": dosage_warnings
        }
    except Exception as e:
        print(f"Error generating US Roadmap: {e}")
        raise HTTPException(status_code=500, detail="오류가 발생하여 정보를 생성하지 못했습니다.")
