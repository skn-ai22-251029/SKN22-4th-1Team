import logging
import asyncio
from .state import AgentState
from services.ai_service_v2 import AIService
from services.drug_service import DrugService
from services.user_service import UserService
from services.supabase_service import SupabaseService
from services.map_service import MapService
from services.ingredient_utils import (
    canonicalize_ingredient_name,
    canonicalize_ingredient_list,
)

logger = logging.getLogger(__name__)


def _normalize_ai_ingredients(ai_ingredients, dur_data):
    """Validate and normalize AI ingredient list against DUR ingredient candidates."""
    if not isinstance(dur_data, list):
        return []

    ordered_names = []
    for item in dur_data:
        if not isinstance(item, dict):
            continue
        name = canonicalize_ingredient_name(item.get("ingredient"))
        if name and name not in ordered_names:
            ordered_names.append(name)

    allowed = set(ordered_names)
    normalized_map = {}

    if isinstance(ai_ingredients, list):
        for ing in ai_ingredients:
            if not isinstance(ing, dict):
                continue
            name = canonicalize_ingredient_name(ing.get("name"))
            if not name or name not in allowed:
                continue
            if name in normalized_map:
                continue
            warning_types = ing.get("dur_warning_types")
            if not isinstance(warning_types, list):
                warning_types = []
            can_take_raw = ing.get("can_take", True)
            if isinstance(can_take_raw, bool):
                can_take = can_take_raw
            elif isinstance(can_take_raw, str):
                can_take = can_take_raw.strip().lower() in ("true", "1", "yes", "y")
            else:
                can_take = bool(can_take_raw)
            normalized_map[name] = {
                "name": name,
                "can_take": can_take,
                "reason": str(ing.get("reason") or ""),
                "dur_warning_types": [str(x) for x in warning_types if isinstance(x, str)],
            }

    # Fill missing ingredients conservatively to avoid silent omissions.
    for name in ordered_names:
        if name not in normalized_map:
            normalized_map[name] = {
                "name": name,
                "can_take": False,
                "reason": "AI 응답에서 누락되어 보수적으로 주의 성분으로 분류했습니다.",
                "dur_warning_types": [],
            }

    return [normalized_map[name] for name in ordered_names]


async def classify_node(state: AgentState) -> AgentState:
    """Classify user query and extract keyword."""
    query = state["query"]
    intent = await AIService.classify_intent(query)

    category = intent.get("category", "invalid")
    keyword = intent.get("keyword", "")
    cache_key = intent.get("cache_key")
    is_cached = False
    cached_payload = {}

    if category == "symptom_recommendation":
        if not cache_key:
            cache_key = await AIService.normalize_symptom_query(query)
        try:
            cached = await SupabaseService.get_symptom_cache(cache_key)
            if cached:
                is_cached = True
                cached_payload = {
                    "final_answer": cached.get("final_answer", ""),
                    "fda_data": cached.get("fda_data") or [],
                    "dur_data": cached.get("dur_data") or [],
                    "ingredients_data": cached.get("ingredients_data") or [],
                }
        except Exception as e:
            logger.warning(f"Symptom cache lookup failed for key '{cache_key}': {e}")

    return {
        "category": category,
        "keyword": keyword,
        "symptom": query if category == "symptom_recommendation" else None,
        "cache_key": cache_key,
        "is_cached": is_cached,
        **cached_payload,
    }


async def retrieve_data_node(state: AgentState) -> AgentState:
    """
    Symptom path: symptom classification -> DB search -> ingredient extraction.
    Product path: product search on FDA.
    """
    category = state["category"]
    keyword = state["keyword"]
    query = state["query"]

    user_profile_data = state.get("user_profile")
    if category == "symptom_recommendation" and not user_profile_data:
        user_info = state.get("user_info")
        if user_info:
            try:
                profile = await UserService.get_profile(user_info)
                if profile:
                    user_profile_data = {
                        "current_medications": profile.current_medications,
                        "allergies": profile.allergies,
                        "chronic_diseases": profile.chronic_diseases,
                    }
            except Exception as e:
                logger.error(f"Error fetching user profile from Supabase: {e}")

    if category == "symptom_recommendation":
        ranked_ingredients = await SupabaseService.search_ingredient_scores_by_symptom(
            keyword=keyword,
            raw_query=query,
            max_rows=5000,
        )
        all_ingredients = [item["ingredient"] for item in ranked_ingredients]

        if not all_ingredients:
            logger.info(
                f"DB symptom search returned no ingredients for '{keyword}'. "
                "Falling back to FDA symptom ingredient search."
            )
            eng_kw = [keyword] if keyword and keyword != "none" else ["pain"]
            all_ingredients = await DrugService.get_ingrs_from_fda_by_symptoms(eng_kw)

            if not all_ingredients:
                synonyms = await AIService.get_symptom_synonyms(keyword or query)
                if synonyms:
                    all_ingredients = await DrugService.get_ingrs_from_fda_by_symptoms(
                        synonyms
                    )

            if not all_ingredients:
                all_ingredients = await AIService.recommend_ingredients_for_symptom(
                    keyword or query
                )

        all_ingredients = canonicalize_ingredient_list(all_ingredients)

        if ranked_ingredients:
            merged_scores = {}
            for item in ranked_ingredients:
                name = canonicalize_ingredient_name(item.get("ingredient"))
                if not name:
                    continue
                merged_scores[name] = merged_scores.get(name, 0) + int(item.get("score", 0) or 0)
            scored_candidates = [
                {"ingredient": name, "score": score}
                for name, score in sorted(merged_scores.items(), key=lambda x: (-x[1], x[0]))
            ]
        else:
            scored_candidates = [{"ingredient": name, "score": 0} for name in all_ingredients]

        fda_ingredients = await AIService.select_direct_symptom_ingredients(
            symptom=keyword or query,
            candidates=scored_candidates,
            top_n=5,
        )
        fda_ingredients = canonicalize_ingredient_list(fda_ingredients)[:5]
        if not fda_ingredients:
            fda_ingredients = all_ingredients[:5]

        logger.info(
            f"Symptom '{keyword}' ingredients extracted={len(all_ingredients)}, "
            f"FDA targets={len(fda_ingredients)}"
        )

        return {
            "all_ingredient_candidates": all_ingredients,
            "ingredient_candidates": fda_ingredients,
            "fda_data": fda_ingredients,
            "user_profile": user_profile_data,
        }

    if category == "product_request":
        target = keyword if keyword and keyword != "none" else query
        fda_data = await DrugService.search_fda(target)
        return {
            "fda_data": fda_data,
            "user_profile": user_profile_data,
        }

    return {"user_profile": user_profile_data}


async def retrieve_fda_products_node(state: AgentState) -> AgentState:
    """FDA product name search based on extracted ingredients."""
    if state["category"] != "symptom_recommendation":
        return {}

    ingredients = state.get("ingredient_candidates") or []
    if not ingredients:
        return {"products_map": {}}

    async def fetch_products(ingr_name: str):
        try:
            result = await MapService.get_us_otc_products_by_ingredient(ingr_name)
            return ingr_name.upper(), result.get("products", [])
        except Exception as e:
            logger.warning(f"Failed to fetch products for '{ingr_name}': {e}")
            return ingr_name.upper(), []

    product_results = await asyncio.gather(*[fetch_products(n) for n in ingredients])
    return {"products_map": dict(product_results)}


async def retrieve_dur_node(state: AgentState) -> AgentState:
    """Extract KR/US DUR data after product lookup."""
    category = state["category"]

    if category == "symptom_recommendation":
        ingredients = state.get("ingredient_candidates") or []
        if not ingredients:
            return {"dur_data": []}
        dur_data = await DrugService.get_enriched_dur_info(ingredients)
        return {"dur_data": dur_data}

    if category == "product_request":
        fda_data = state.get("fda_data")
        if not fda_data or not isinstance(fda_data, dict):
            return {"dur_data": []}
        ingrs = fda_data.get("active_ingredients", "")
        dur_data = await DrugService.get_dur_by_ingr(ingrs)
        return {"dur_data": dur_data}

    return {"dur_data": []}


async def generate_symptom_answer_node(state: AgentState) -> AgentState:
    """Generate final symptom response using DUR + FDA product lookup result."""
    symptom = state["symptom"]
    dur_data = state.get("dur_data") or []
    fda_data = state.get("fda_data", [])
    products_map = state.get("products_map") or {}

    if state.get("is_cached", False):
        return {
            "final_answer": state.get("final_answer", ""),
            "dur_data": dur_data,
            "fda_data": fda_data,
            "ingredients_data": state.get("ingredients_data", []),
        }

    if not dur_data:
        fallback_query = (
            f"The user asked about '{symptom}' but I couldn't find specific drugs in the DB/FDA/DUR flow. "
            f"Please provide general medical advice or common over-the-counter ingredients for this symptom. "
            f"(User query: {state['query']})"
        )
        answer = await AIService.generate_general_answer(fallback_query)
        prefix = "해당 증상에 대한 DB/FDA/DUR 기반 정보를 찾기 어려워 일반 가이드를 제공합니다.\n\n"
        return {"final_answer": prefix + answer, "ingredients_data": []}

    ai_result = await AIService.generate_symptom_answer(
        symptom, dur_data, state.get("user_profile")
    )

    is_invalid_payload = not isinstance(ai_result, dict) or not isinstance(
        ai_result.get("ingredients"), list
    )
    if is_invalid_payload:
        retry_result = await AIService.generate_symptom_answer(
            symptom, dur_data, state.get("user_profile")
        )
        if isinstance(retry_result, dict):
            ai_result = retry_result

    if not isinstance(ai_result, dict):
        return {
            "final_answer": str(ai_result),
            "dur_data": dur_data,
            "ingredients_data": [],
        }

    summary = ai_result.get("summary", "")
    if not isinstance(summary, str) or not summary.strip():
        summary = "요청 증상에 대해 성분별 안전성과 주의사항을 정리했습니다."
    ai_ingredients = _normalize_ai_ingredients(ai_result.get("ingredients", []), dur_data)

    dur_map = {item["ingredient"].upper(): item for item in dur_data}

    ingredients_data = []
    for ing in ai_ingredients:
        name = ing.get("name", "").upper()
        dur_item = dur_map.get(name, {})
        entry = {
            "name": name,
            "can_take": ing.get("can_take", True),
            "reason": ing.get("reason", ""),
            "dur_warning_types": ing.get("dur_warning_types", []),
            "kr_durs": dur_item.get("kr_durs", []),
            "fda_warning": dur_item.get("fda_warning", None),
            "products": products_map.get(name, []) if ing.get("can_take", False) else [],
        }
        ingredients_data.append(entry)

    try:
        await SupabaseService.set_symptom_cache(
            query_text=state.get("cache_key") or state.get("query", ""),
            category="symptom_recommendation",
            fda_data=fda_data if isinstance(fda_data, list) else [],
            dur_data=dur_data,
            final_answer=summary,
            recommended_ingredients=[x.get("name") for x in ingredients_data if x.get("name")],
        )
    except Exception as e:
        logger.warning(f"Failed to save symptom cache: {e}")

    return {
        "final_answer": summary,
        "dur_data": dur_data,
        "fda_data": fda_data,
        "ingredients_data": ingredients_data,
    }


async def generate_product_answer_node(state: AgentState) -> AgentState:
    """Generate answer for product queries."""
    fda_data = state.get("fda_data")
    dur_data = state.get("dur_data") or []

    if not fda_data:
        return {"final_answer": "해당 의약품 정보를 찾을 수 없습니다."}

    brand_name = fda_data.get("brand_name")
    indications = fda_data.get("indications")

    answer = f"**{brand_name}** 정보입니다.\n\n**효능/효과**:\n{indications}\n\n**DUR/주의사항**:\n"
    for d in dur_data:
        answer += f"- {d['ingr_name']} ({d['type']}): {d['warning_msg']}\n"

    return {"final_answer": answer}


async def generate_general_answer_node(state: AgentState) -> AgentState:
    answer = await AIService.generate_general_answer(state["query"])
    return {"final_answer": answer}


async def generate_error_node(state: AgentState) -> AgentState:
    return {
        "final_answer": "질문을 이해하지 못했거나 의약품과 관련 없는 요청입니다."
    }
