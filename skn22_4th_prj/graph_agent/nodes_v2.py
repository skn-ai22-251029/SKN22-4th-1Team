import logging
import asyncio
import re
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


SYMPTOM_TO_FDA_TERMS = {
    "두통": ["headache", "pain relief"],
    "편두통": ["migraine", "headache"],
    "알레르기": ["allergy", "allergic reaction", "antihistamine"],
    "기침": ["cough", "cold", "nasal congestion", "sinus congestion"],
    "감기": ["cold"],
    "발열": ["fever"],
    "소화불량": ["indigestion"],
    "복통": ["stomachache", "abdominal pain"],
    "염좌": ["sprain"],
    "찰과상": ["wound", "skin abrasion"],
    "화상": ["burn"],
    "곤충교상": ["insect bite"],
}


def _to_fda_symptom_terms(symptom_term: str):
    token = str(symptom_term or "").strip().lower()
    if not token:
        return []
    return SYMPTOM_TO_FDA_TERMS.get(token, [])


def _merge_unique_terms(*groups):
    merged = []
    seen = set()
    for group in groups:
        if not isinstance(group, list):
            continue
        for value in group:
            token = str(value or "").strip().lower()
            if not token or token in seen:
                continue
            seen.add(token)
            merged.append(token)
    return merged


def _has_user_risk_profile(user_profile):
    if not isinstance(user_profile, dict):
        return False

    for key in ("current_medications", "allergies", "chronic_diseases"):
        value = str(user_profile.get(key) or "").strip().lower()
        if value and value not in {
            "none",
            "\uc5c6\uc74c",
            "\uc5c6\uc5b4\uc694",
            "n/a",
            "na",
            "x",
        }:
            return True
    return False


def _to_profile_text(value):
    text = str(value or "").strip()
    return text if text else "\uc785\ub825 \uc5c6\uc74c"


def _looks_mojibake(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return True

    if "??" in value or "\ufffd" in value:
        return True

    cjk = len(re.findall(r"[\u4E00-\u9FFF]", value))
    hangul = len(re.findall(r"[\uAC00-\uD7A3]", value))
    ascii_alpha = len(re.findall(r"[A-Za-z]", value))
    if cjk >= 3 and hangul == 0 and ascii_alpha < 3:
        return True

    suspicious_tokens = ("?", "?", "?", "?", "?", "?", "??", "?")
    if any(token in value for token in suspicious_tokens) and hangul < 2:
        return True

    return False


def _fallback_reason(can_take: bool, warning_types) -> str:
    warnings = [str(x).strip() for x in (warning_types or []) if str(x).strip()]
    if can_take is False:
        return (
            "DUR \uc815\ubcf4\uc0c1 \ubcf5\uc6a9\ud558\uba74 "
            "\uc704\ud5d8\ud558\ub2e4\uace0 \uc548\ub0b4\ub418\uace0 \uc788\uc2b5\ub2c8\ub2e4."
        )
    if warnings:
        return (
            f"DUR \uc8fc\uc758 \ud56d\ubaa9({', '.join(warnings[:3])}) "
            "\uae30\uc900\uc73c\ub85c \uc8fc\uc758\uac00 \ud544\uc694\ud55c \uc131\ubd84\uc785\ub2c8\ub2e4."
        )
    return (
        "\uac1c\uc778 \uac74\uac15\uc815\ubcf4(\ubcf5\uc6a9\uc57d/\uc54c\ub808\ub974\uae30/\uae30\uc800\uc9c8\ud658) "
        "\uae30\uc900\uc5d0\uc11c \uc77c\ubc18 \ubcf5\uc6a9 \uac00\ub2a5\uc73c\ub85c \uc548\ub0b4\ub429\ub2c8\ub2e4."
    )


def _build_profile_reflection_tail(user_profile, ingredients_data):
    if not isinstance(user_profile, dict):
        return ""

    meds = _to_profile_text(user_profile.get("current_medications"))
    allergies = _to_profile_text(user_profile.get("allergies"))
    diseases = _to_profile_text(user_profile.get("chronic_diseases"))

    blocked = []
    caution = []
    for ing in ingredients_data or []:
        if not isinstance(ing, dict):
            continue
        name = str(ing.get("name") or "").strip()
        if not name:
            continue
        can_take = ing.get("can_take", True)
        warnings = ing.get("dur_warning_types") or []
        if can_take is False:
            blocked.append(name)
        elif warnings:
            caution.append(name)

    if blocked:
        reflection = (
            f"\uac1c\uc778 \uac74\uac15\uc815\ubcf4 \uae30\uc900\uc73c\ub85c "
            f"\ubcf5\uc6a9 \uc81c\ud55c \uc131\ubd84\uc774 \ud655\uc778\ub418\uc5c8\uc2b5\ub2c8\ub2e4: "
            f"{', '.join(blocked[:5])}"
        )
    elif caution:
        reflection = (
            f"\uac1c\uc778 \uac74\uac15\uc815\ubcf4 \uae30\uc900\uc73c\ub85c "
            f"\uc8fc\uc758\uac00 \ud544\uc694\ud55c \uc131\ubd84\uc774 \ud655\uc778\ub418\uc5c8\uc2b5\ub2c8\ub2e4: "
            f"{', '.join(caution[:5])}"
        )
    else:
        reflection = (
            "\uc785\ub825\ud55c \uac1c\uc778 \uac74\uac15\uc815\ubcf4 \uae30\uc900\uc5d0\uc11c "
            "\uc911\ub300\ud55c \ubcf5\uc6a9 \uc81c\ud55c \uc131\ubd84\uc740 "
            "\ud655\uc778\ub418\uc9c0 \uc54a\uc558\uc2b5\ub2c8\ub2e4."
        )

    return (
        "\n\n[\uac1c\uc778 \uac74\uac15\uc815\ubcf4 \ubc18\uc601 \uc694\uc57d]\n"
        f"- \ubcf5\uc6a9 \uc911\uc778 \uc57d: {meds}\n"
        f"- \uc54c\ub808\ub974\uae30: {allergies}\n"
        f"- \uae30\uc800\uc9c8\ud658: {diseases}\n"
        f"- \ubc18\uc601 \uacb0\uacfc: {reflection}"
    )

def _normalize_ai_ingredients(ai_ingredients, dur_data):
    """Build stable output entries from preselected DUR ingredients.

    Ingredient validity is decided upstream. Here we only normalize output fields.
    """
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

    # Fill missing ingredients with neutral defaults to keep output stable.
    for name in ordered_names:
        if name not in normalized_map:
            normalized_map[name] = {
                "name": name,
                "can_take": True,
                "reason": "개별 복용 판정 정보가 없어 일반 주의 안내를 제공합니다.",
                "dur_warning_types": [],
            }

    return [normalized_map[name] for name in ordered_names]


async def classify_node(state: AgentState) -> AgentState:
    """Classify user query and extract keyword."""
    query = state["query"]
    intent = await AIService.classify_intent(query)

    category = intent.get("category", "invalid")
    keyword = intent.get("keyword", "")
    query_l = str(query or "").strip().lower()

    # Heuristic safeguard: allergy-like symptom queries must stay on symptom path.
    if any(token in query_l for token in ["알레르기", "allergy", "allergic"]):
        category = "symptom_recommendation"
        if not keyword or keyword == "none":
            keyword = "알레르기"

    return {
        "category": category,
        "keyword": keyword,
        "symptom": query if category == "symptom_recommendation" else None,
        "cache_key": None,
        "is_cached": False,
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
                    applied_allergies = (
                        getattr(profile, "applied_allergies", None) or profile.allergies
                    )
                    applied_chronic_diseases = (
                        getattr(profile, "applied_chronic_diseases", None)
                        or profile.chronic_diseases
                    )
                    food_allergy_detail = (
                        str(getattr(profile, "food_allergy_detail", "") or "").strip()
                    )
                    if food_allergy_detail and "상세정보:" not in str(applied_allergies or ""):
                        applied_allergies = (
                            f"{applied_allergies} | 상세정보: {food_allergy_detail}"
                            if applied_allergies
                            else f"상세정보: {food_allergy_detail}"
                        )
                    user_profile_data = {
                        "current_medications": profile.current_medications,
                        "allergies": applied_allergies,
                        "chronic_diseases": applied_chronic_diseases,
                    }
            except Exception as e:
                logger.error(f"Error fetching user profile from Supabase: {e}")

    if category == "symptom_recommendation":
        db_symptom_term = await AIService.canonicalize_symptom_term(
            query=query,
            hint_keyword=keyword,
        )
        db_symptom_term = (db_symptom_term or keyword or query).strip()

        ranked_ingredients = await SupabaseService.search_ingredient_scores_by_symptom(
            keyword=db_symptom_term,
            raw_query=query,
            max_rows=5000,
        )
        all_ingredients = [item["ingredient"] for item in ranked_ingredients]
        eng_kw = _to_fda_symptom_terms(db_symptom_term)
        if not eng_kw:
            eng_kw = _to_fda_symptom_terms(keyword)
        if not eng_kw:
            eng_kw = ["pain"]
        synonyms = await AIService.get_symptom_synonyms(keyword or query)
        search_terms = _merge_unique_terms(eng_kw, synonyms)
        logger.info(
            "FDA symptom ingredient terms: %s",
            ", ".join(search_terms),
        )
        fda_candidates = await DrugService.get_ingrs_from_fda_by_symptoms(
            search_terms
        )
        fda_candidates = canonicalize_ingredient_list(fda_candidates)

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
            scored_candidates = []

        # Primary source of truth:
        # unified_drug_info.efficacy matches symptom -> extract ingredients from main_ingr_eng.
        # Then reduce noisy combo ingredients by selecting symptom-direct top ingredients.
        selected_ingredients = []
        if scored_candidates:
            selected_ingredients = await AIService.select_direct_symptom_ingredients(
                symptom=db_symptom_term or query,
                candidates=scored_candidates,
                top_n=10,
            )
            selected_ingredients = canonicalize_ingredient_list(selected_ingredients)[:10]
            if not selected_ingredients:
                selected_ingredients = [
                    item["ingredient"]
                    for item in scored_candidates
                    if item.get("ingredient")
                ][:10]
            if selected_ingredients and fda_candidates:
                selected_ingredients = canonicalize_ingredient_list(
                    selected_ingredients
                )[:10]
                overlap = set(selected_ingredients).intersection(set(fda_candidates))
                fda_unique = [ingr for ingr in fda_candidates if ingr not in selected_ingredients]
                supplement_slots = min(3, len(fda_unique))
                if supplement_slots > 0:
                    db_keep = max(10 - supplement_slots, 0)
                    merged_selected = selected_ingredients[:db_keep] + fda_unique[:supplement_slots]
                    for token in selected_ingredients[db_keep:]:
                        if token not in merged_selected:
                            merged_selected.append(token)
                    for token in fda_unique[supplement_slots:]:
                        if token not in merged_selected:
                            merged_selected.append(token)
                    selected_ingredients = canonicalize_ingredient_list(merged_selected)[:10]
                    logger.info(
                        "Symptom candidate supplementation applied: overlap=%d supplemented=%d",
                        len(overlap),
                        supplement_slots,
                    )

        if not selected_ingredients:
            logger.info(
                f"DB symptom search returned no ingredients for '{db_symptom_term}'. "
                "Falling back to FDA symptom ingredient search."
            )
            all_ingredients = list(fda_candidates)

            if not all_ingredients:
                all_ingredients = await AIService.recommend_ingredients_for_symptom(
                    keyword or query
                )

            all_ingredients = canonicalize_ingredient_list(all_ingredients)
            selected_ingredients = all_ingredients[:10]
        else:
            all_ingredients = canonicalize_ingredient_list(
                [item["ingredient"] for item in scored_candidates] + list(fda_candidates)
            )

        fda_ingredients = selected_ingredients[:5]
        backup_ingredients = selected_ingredients[5:10]

        logger.info(
            f"Symptom raw='{query}', db_term='{db_symptom_term}', keyword='{keyword}' "
            f"ingredients extracted={len(all_ingredients)}, "
            f"primary_targets={len(fda_ingredients)}, backup_targets={len(backup_ingredients)}"
        )
        logger.info(
            "Symptom selected ingredients (top10): %s",
            ", ".join(selected_ingredients) if selected_ingredients else "(none)",
        )
        logger.info(
            "Symptom FDA candidates (top10): %s",
            ", ".join(fda_candidates[:10]) if fda_candidates else "(none)",
        )

        return {
            "all_ingredient_candidates": all_ingredients,
            "ingredient_candidates": selected_ingredients,
            "backup_ingredient_candidates": backup_ingredients,
            "symptom_term": db_symptom_term,
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
    # Deprecated path: product lookup moved to answer_symptom for can_take=true ingredients only.
    return {"products_map": {}}


async def retrieve_dur_node(state: AgentState) -> AgentState:
    """Extract KR/US DUR data after product lookup."""
    category = state["category"]

    if category == "symptom_recommendation":
        ingredients = state.get("ingredient_candidates") or []
        if not ingredients:
            return {"dur_data": []}
        # Initial response: KR DUR only.
        # US warning and product details are loaded asynchronously from a follow-up API.
        dur_data = await DrugService.get_kr_dur_info(ingredients)
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
    products_map = {}

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

    should_retry = not isinstance(ai_result, dict) or ("ingredients" not in ai_result)
    if should_retry:
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
        summary = "요청 증상 관련 성분 안전성과 주의사항을 정리했습니다."
    ai_ingredients = _normalize_ai_ingredients(ai_result.get("ingredients", []), dur_data)

    # Policy: without user risk profile, do not classify ingredients as "cannot take".
    # can_take=false is only allowed when there is user-specific risk information to evaluate.
    has_user_risk = _has_user_risk_profile(state.get("user_profile"))
    if not has_user_risk:
        for ing in ai_ingredients:
            ing["can_take"] = True
            reason = str(ing.get("reason") or "").strip()
            if not reason:
                ing["reason"] = (
                    "\uac1c\uc778 \uac74\uac15\uc815\ubcf4(\ubcf5\uc6a9\uc57d/"
                    "\uc54c\ub808\ub974\uae30/\uae30\uc800\uc9c8\ud658) \ubbf8\uc785\ub825 \uc0c1\ud0dc\ub85c "
                    "\uc77c\ubc18 \ubcf5\uc6a9 \uac00\ub2a5 \uae30\uc900\uc73c\ub85c \uc548\ub0b4\ub429\ub2c8\ub2e4."
                )

    dur_map = {item["ingredient"].upper(): item for item in dur_data}
    ai_map = {}
    for ing in ai_ingredients:
        name = str(ing.get("name") or "").strip().upper()
        if not name:
            continue
        ai_map[name] = ing

    # Keep rendering order aligned with DB-ranked ingredients (1~10).
    # This guarantees initial UI shows rank 1~5 first, then 6~10 as replacement pool.
    ranked_ingredients = state.get("ingredient_candidates") or []
    ordered_names = [str(x).strip().upper() for x in ranked_ingredients if str(x).strip()]
    if not ordered_names:
        ordered_names = [str(item.get("ingredient") or "").strip().upper() for item in dur_data]
        ordered_names = [x for x in ordered_names if x]

    ingredients_data = []
    for name in ordered_names:
        dur_item = dur_map.get(name, {})
        ai_item = ai_map.get(name, {})
        can_take = ai_item.get("can_take", True)
        warning_types = ai_item.get("dur_warning_types", [])
        reason = str(ai_item.get("reason") or "").strip()
        if _looks_mojibake(reason):
            reason = _fallback_reason(can_take, warning_types)
        if can_take is False:
            risk_prefix = (
                "DUR \uc815\ubcf4\uc0c1 \ubcf5\uc6a9\ud558\uba74 "
                "\uc704\ud5d8\ud558\ub2e4\uace0 \uc548\ub0b4\ub418\uace0 \uc788\uc2b5\ub2c8\ub2e4."
            )
            if not reason:
                reason = risk_prefix
            elif risk_prefix not in reason:
                reason = f"{risk_prefix} {reason}"
        elif not reason:
            reason = _fallback_reason(can_take, warning_types)
        entry = {
            "name": name,
            "can_take": can_take,
            "reason": reason,
            "dur_warning_types": warning_types,
            "kr_durs": dur_item.get("kr_durs", []),
            "fda_warning": dur_item.get("fda_warning", None),
            "products": products_map.get(name, []),
        }
        ingredients_data.append(entry)

    profile_tail = _build_profile_reflection_tail(state.get("user_profile"), ingredients_data)
    final_answer = summary + profile_tail if profile_tail else summary

    return {
        "final_answer": final_answer,
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
