import os
import logging
import httpx
import re
import asyncio
from services.ai_service_v2 import AIService

logger = logging.getLogger(__name__)

class MapService:
    @classmethod
    async def find_nearby_pharmacies(cls, lat: float, lng: float):
        """
        # Google Maps Nearby Search API 활용 (현재 사용자 요청으로 주석 처리됨)
        # api_key = os.getenv("GOOGLE_MAPS_API_KEY")
        # url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
        # params = {
        #     "location": f"{lat},{lng}",
        #     "radius": 1500, # 1.5km
        #     "type": "pharmacy",
        #     "key": api_key,
        #     "language": "ko"
        # }
        # async with httpx.AsyncClient() as client:
        #     resp = await client.get(url, params=params)
        #     return resp.json().get("results", [])
        """
        return []

    @classmethod
    async def get_us_otc_products_by_ingredient(cls, ingredient: str):
        """
        특정 주성분이 포함된 미국 내 가용 OTC 제품명(Brand Name) 및 기전 전수 리스트업
        """
        # openFDA에서 substance_name 혹은 generic_name으로 검색. (limit=50)
        OTC_FILTER = 'openfda.product_type:"HUMAN OTC DRUG"'
        url = (
            f'https://api.fda.gov/drug/label.json'
            f'?search=(openfda.substance_name:"{ingredient}"+OR+openfda.generic_name:"{ingredient}")'
            f'+AND+{OTC_FILTER}'
            f'&limit=50'
        )
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    return {"ingredient": ingredient, "products": [], "message": "FDA API에서 제품을 찾을 수 없습니다."}
                
                data = response.json().get('results', [])
                
                products_info = []
                for item in data:
                    openfda = item.get('openfda', {})
                    brand_name = openfda.get('brand_name', [])
                    if not brand_name:
                        continue
                    
                    # 기전(Mechanism of Action) 또는 사용목적(purpose) 추출
                    purpose = item.get('purpose', ["Description not available."])[0]
                    active_ingr = item.get('active_ingredient', ["Unknown"])[0]
                    
                    products_info.append({
                        "brand_name": brand_name[0],
                        "purpose": purpose,
                        "active_ingredient": active_ingr
                    })
                
                # 중복 제거 (brand_name 기준)
                unique_products = {prod['brand_name']: prod for prod in products_info}.values()
                sorted_products = sorted(list(unique_products), key=lambda x: x['brand_name'])
                
                # 목적(purpose) 한국어 번역
                purposes_to_translate = [p['purpose'] for p in sorted_products]
                translated_purposes = await AIService.translate_purposes(purposes_to_translate)
                
                for i, prod in enumerate(sorted_products):
                    if i < len(translated_purposes):
                        prod['purpose'] = translated_purposes[i]
                
                return {
                    "ingredient": ingredient,
                    "products": sorted_products,
                    "count": len(sorted_products)
                }
            except Exception as e:
                logger.error(f"Error fetching FDA products for '{ingredient}': {e}")
                return {"ingredient": ingredient, "products": [], "error": str(e)}

    @classmethod
    async def find_optimal_us_products(cls, ingredients: list):
        """
        복합 주성분 제품(Combination Drugs) 분해 및 듀얼 매치 알고리즘
        1단계: Full Match (모든 성분이 포함된 복합제 검색)
        2단계: Component Match (없을 경우, 개별 성분별 단일제 큐레이션)
        """
        if not ingredients:
            return {"match_type": "NONE", "recommendations": []}

        # Full Match 검색 시도 (단순화를 위해 각 성분이 모두 포함되는지 AND 검색)
        OTC_FILTER = 'openfda.product_type:"HUMAN OTC DRUG"'
        search_query = "+AND+".join(
            [f'(openfda.substance_name:"{ingr}"+OR+openfda.generic_name:"{ingr}")' for ingr in ingredients]
        )
        url = f'https://api.fda.gov/drug/label.json?search={search_query}+AND+{OTC_FILTER}&limit=10'
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                res = await client.get(url)
                if res.status_code == 200 and res.json().get('results'):
                    # Full Match 성공
                    results = res.json().get('results', [])
                    products = []
                    for item in results:
                        brand_name = item.get('openfda', {}).get('brand_name', ['Unknown'])[0]
                        purpose = item.get('purpose', ['No purpose specified.'])[0]
                        active_ingr = item.get('active_ingredient', ['Unknown'])[0]
                        products.append({
                            "brand_name": brand_name, 
                            "purpose": purpose,
                            "active_ingredient": active_ingr
                        })
                    
                    # 목적(purpose) 한국어 번역
                    purposes_to_translate = [p['purpose'] for p in products]
                    translated_purposes = await AIService.translate_purposes(purposes_to_translate)
                    
                    for i, prod in enumerate(products):
                        if i < len(translated_purposes):
                            prod['purpose'] = translated_purposes[i]
                    
                    return {
                        "match_type": "FULL_MATCH",
                        "description": "모든 성분이 일치하는 미국 복합제 우선 추천",
                        "recommendations": products
                    }
            except Exception as e:
                logger.error(f"Full match search error: {e}")
        
        # Component Match (Full Match 실패 또는 데이터 부족 시 개별 검색)
        component_recommendations = []
        for ingr in ingredients:
            ingr_products = await cls.get_us_otc_products_by_ingredient(ingr)
            component_recommendations.append(ingr_products)
            
        return {
            "match_type": "COMPONENT_MATCH",
            "description": "완전 일치 복합제가 없어 각 성분별 단일제 그룹을 큐레이션하여 추천합니다.",
            "recommendations": component_recommendations
        }

    @classmethod
    def generate_pharmacist_card(cls, ingredients: list, dosage_form: str = "Tablet/Capsule"):
        """
        약사 소통 브릿지 (Pharmacist Comm. Card) 생성
        """
        ingr_str = ", ".join(ingredients)
        
        card = {
            "title": "Pharmacist Communication Card",
            "active_ingredients": ingredients,
            "desired_dosage_form": dosage_form,
            "english_guide": [
                f"Hello, I am looking for an OTC product containing these active ingredients: {ingr_str}.",
                f"I prefer the '{dosage_form}' form if available.",
                "Could you please recommend the closest match you have in stock?"
            ]
        }
        return card