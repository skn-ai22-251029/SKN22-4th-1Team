import os
import logging
import asyncio
import json
from collections import Counter
from functools import lru_cache

from django.shortcuts import render
from django.http import HttpResponse, JsonResponse

from graph_agent.builder_v2 import build_graph

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_graph():
    return build_graph()


def _normalize_dur_entries(dur_entries):
    normalized = []
    for item in dur_entries or []:
        if not isinstance(item, dict):
            continue

        dur_type = str(item.get("type") or "주의").strip()
        ingredient = str(
            item.get("ingr_name") or item.get("ingredient") or item.get("name") or ""
        ).strip()
        warning = str(
            item.get("warning_msg") or item.get("warning") or item.get("message") or ""
        ).strip()

        normalized.append(
            {
                "type": dur_type or "주의",
                "ingredient": ingredient,
                "warning": warning,
            }
        )
    return normalized


def _guidance_by_dur_type(dur_type):
    t = (dur_type or "").strip()
    t_lower = t.lower()
    is_combined = ("interaction" in t_lower) or ("combined" in t_lower) or ("병용" in t)
    is_contra = ("contra" in t_lower) or ("금기" in t)
    is_caution = ("caution" in t_lower) or ("주의" in t)

    if is_combined and is_contra:
        return "다른 성분과의 병용이 제한된다고 안내되고 있습니다."
    if is_combined and is_caution:
        return "다른 성분과 병용 시 이상반응 가능성이 있다고 안내되고 있습니다."
    if "pregnan" in t_lower or "임부" in t or "임신" in t:
        return "임신 중 사용 제한 또는 주의가 필요하다고 안내되고 있습니다."
    if "elder" in t_lower or "geriatric" in t_lower or "노인" in t or "고령" in t:
        return "고령자에서 주의가 필요하다고 안내되고 있습니다."
    if (
        "adolescent" in t_lower
        or "pediatric" in t_lower
        or "age" in t_lower
        or "연령" in t
        or "청소년" in t
        or "소아" in t
    ):
        return "연령 기준에 따른 사용 제한 또는 주의가 필요하다고 안내되고 있습니다."
    if "dose" in t_lower or "용량" in t:
        return "권장 용량 범위를 준수해야 한다고 안내되고 있습니다."
    if "duration" in t_lower or "기간" in t:
        return "권장 투여 기간을 넘기지 않도록 안내되고 있습니다."
    if (
        "disease" in t_lower
        or "condition" in t_lower
        or "질환" in t
        or "kidney" in t_lower
        or "liver" in t_lower
        or "신장" in t
        or "간" in t
    ):
        return "기저 질환 여부에 따른 사용 주의가 필요하다고 안내되고 있습니다."
    if is_contra:
        return "금기 항목으로 안내되고 있어 전문가 확인이 권고됩니다."
    if is_caution:
        return "주의 항목으로 안내되고 있어 전문가 확인이 권고됩니다."
    return "개인 상태에 따라 적용 기준이 달라질 수 있다고 안내되고 있습니다."


def _build_dur_summary(dur_entries, limit=5):
    entries = _normalize_dur_entries(dur_entries)
    if not entries:
        return {
            "count": 0,
            "headline": "",
            "type_summary": "",
            "lines": [],
            "has_more": False,
        }

    type_counter = Counter(entry["type"] for entry in entries if entry["type"])
    top_types = ", ".join(
        [f"{dur_type} {count}건" for dur_type, count in type_counter.most_common(3)]
    )

    lines = []
    for entry in entries[:limit]:
        ingredient = entry["ingredient"] or "해당 성분"
        guidance = _guidance_by_dur_type(entry["type"])
        line = (
            f"{ingredient}: DUR 기준상 '{entry['type']}' 항목으로 안내되고 있습니다. "
            f"{guidance} 세부 적용 여부는 의사 또는 약사 상담을 통해 확인이 권고됩니다."
        )
        if entry["warning"]:
            warning = entry["warning"]
            if len(warning) > 100:
                warning = warning[:100].rstrip() + "..."
            line = f"{line} ({warning})"
        lines.append(line)

    return {
        "count": len(entries),
        "headline": f"DUR 안내 항목 {len(entries)}건이 확인되었습니다.",
        "type_summary": top_types,
        "lines": lines,
        "has_more": len(entries) > limit,
    }


def home(request):
    user = request.session.get("supabase_user")
    return render(request, "index.html", {"user": user})


async def smart_search(request):
    query = request.GET.get("q") or request.POST.get("q")
    if not query:
        return HttpResponse("<script>alert('검색어를 입력하세요.'); history.back();</script>")

    logger.info(f"LangGraph User Query: {query}")

    user_info = request.session.get("supabase_user")
    inputs = {"query": query, "user_info": user_info}

    try:
        result = await get_graph().ainvoke(inputs)
    except Exception as e:
        logger.error(f"Graph Execution Error: {e}")
        return render(request, "error.html", {"message": f"처리 중 오류가 발생했습니다: {str(e)}"})

    category = result.get("category")
    final_answer = result.get("final_answer", "")

    if category == "symptom_recommendation":
        dur_data = result.get("dur_data", [])
        return render(
            request,
            "symptom_result.html",
            {
                "symptom": query,
                "answer": final_answer,
                "ingredients_data": result.get("ingredients_data", []),
                "dur_details": dur_data,
                "dur_summary": _build_dur_summary(dur_data),
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
            },
        )

    if category == "product_request":
        fda = result.get("fda_data")
        dur = result.get("dur_data", [])

        if not fda:
            return render(
                request,
                "error.html",
                {"message": final_answer or f"'{query}' 관련 정보를 찾을 수 없습니다."},
            )

        return render(
            request,
            "search_result.html",
            {
                "drug_name": fda.get("brand_name", query),
                "ingredients": fda.get("active_ingredients"),
                "us_guideline": fda,
                "kr_dur": dur,
                "dur_count": len(dur),
                "dur_summary": _build_dur_summary(dur),
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
            },
        )

    if category == "general_medical":
        return render(
            request,
            "symptom_result.html",
            {
                "symptom": query,
                "answer": final_answer,
                "dur_details": [],
                "dur_summary": _build_dur_summary([]),
                "maps_key": os.getenv("GOOGLE_MAPS_API_KEY"),
            },
        )

    return render(
        request,
        "error.html",
        {"message": final_answer or "요청을 처리할 수 없습니다."},
    )


async def pharmacy_api(request):
    lat = float(request.GET.get("lat", 0))
    lng = float(request.GET.get("lng", 0))

    from services.map_service import MapService

    try:
        results = await MapService.find_nearby_pharmacies(lat, lng)
        return JsonResponse({"status": "success", "results": results})
    except Exception as e:
        logger.error(f"Error fetching pharmacies: {e}")
        return JsonResponse({"status": "error", "message": str(e)})


async def symptom_products_api(request):
    raw = request.GET.get("ingredients", "").strip()
    symptom = (request.GET.get("symptom") or "").strip()
    debug_mode = str(request.GET.get("debug") or "").strip().lower() in {"1", "true", "yes", "y", "on"}
    ingredients = [x.strip().upper() for x in raw.split(",") if x.strip()]
    if not ingredients:
        return JsonResponse({"status": "error", "message": "ingredients is required"}, status=400)

    from services.map_service import MapService
    from services.drug_service import DrugService
    from services.ai_service_v2 import AIService

    semaphore = asyncio.Semaphore(3)
    target_visible_products = 3
    candidate_fetch_limit = 10
    max_extra_component_lookups = 24
    max_excluded_reason_items = 4

    async def fetch_one(ingr):
        async with semaphore:
            try:
                products_kwargs = {"limit": candidate_fetch_limit}
                if symptom:
                    products_kwargs["symptom"] = symptom
                products_task = MapService.get_us_otc_products_by_ingredient(
                    ingr, **products_kwargs
                )
                warning_task = DrugService.get_fda_warnings_by_ingr(ingr)
                products_res, us_warning_raw = await asyncio.gather(products_task, warning_task)
                return {
                    "ingredient": ingr,
                    "products": products_res.get("products", []),
                    "us_warning_raw": us_warning_raw,
                    "diagnostics": products_res.get("diagnostics", {}),
                }
            except Exception as e:
                logger.warning(f"symptom_products_api failed for '{ingr}': {e}")
                return {
                    "ingredient": ingr,
                    "products": [],
                    "us_warning_raw": None,
                    "diagnostics": {"ingredient": ingr, "error": str(e)},
                }

    async def attach_other_component_dur_guidance(payload_items):
        """Analyze other active components with KR DUR and filter risky products."""
        unique_components = set()
        for payload in payload_items or []:
            for product in payload.get("products") or []:
                for token in product.get("other_active_ingredients") or []:
                    name = str(token or "").strip().upper()
                    if name:
                        unique_components.add(name)

        extra_dur_map = {}
        if unique_components:
            capped_components = sorted(unique_components)[:max_extra_component_lookups]
            extra_dur_data = await DrugService.get_kr_dur_info(capped_components)
            for row in extra_dur_data or []:
                name = str((row or {}).get("ingredient") or "").strip().upper()
                kr_durs = (
                    (row or {}).get("kr_durs")
                    if isinstance((row or {}).get("kr_durs"), list)
                    else []
                )
                if not name or not kr_durs:
                    continue

                risk_types = []
                risk_summary = ""
                for dur in kr_durs:
                    if not isinstance(dur, dict):
                        continue
                    dur_type = str(dur.get("type") or "").strip()
                    if dur_type:
                        risk_types.append(dur_type)
                    if not risk_summary:
                        risk_summary = str(dur.get("warning") or "").strip()

                risk_types = sorted(set([x for x in risk_types if x]))
                if len(risk_summary) > 140:
                    risk_summary = risk_summary[:137].rstrip() + "..."

                extra_dur_map[name] = {
                    "has_dur_risk": True,
                    "dur_risk_types": risk_types[:3],
                    "dur_risk_summary": risk_summary,
                }

        for payload in payload_items or []:
            products = (
                payload.get("products")
                if isinstance(payload.get("products"), list)
                else []
            )
            safe_products = []
            excluded_products = []

            for product in products:
                components = (
                    product.get("other_active_components")
                    if isinstance(product.get("other_active_components"), list)
                    else []
                )
                enriched_components = []
                risk_components = []

                for comp in components:
                    if not isinstance(comp, dict):
                        continue
                    comp_name = str(comp.get("name") or "").strip().upper()
                    meta = extra_dur_map.get(comp_name, {})
                    has_risk = bool(meta.get("has_dur_risk"))
                    if has_risk:
                        risk_components.append(
                            {
                                "name": comp_name,
                                "types": meta.get("dur_risk_types", []),
                                "summary": meta.get("dur_risk_summary", ""),
                            }
                        )

                    enriched_components.append(
                        {
                            **comp,
                            "has_dur_risk": has_risk,
                            "dur_risk_types": meta.get("dur_risk_types", []),
                            "dur_risk_summary": meta.get("dur_risk_summary", ""),
                        }
                    )

                if enriched_components:
                    product["other_active_components"] = enriched_components

                if risk_components:
                    preview_names = ", ".join([c["name"] for c in risk_components[:3]])
                    product["has_other_active_dur_risk"] = True
                    product["other_active_dur_notice"] = (
                        f"추가 주성분 DUR 위험으로 제외: {preview_names}"
                    )
                    excluded_products.append(
                        {
                            "brand_name": str(product.get("brand_name") or "Unknown Product"),
                            "risk_components": risk_components[:3],
                            "reason": product["other_active_dur_notice"],
                        }
                    )
                    continue

                product["has_other_active_dur_risk"] = False
                safe_products.append(product)

            payload["products"] = safe_products[:target_visible_products]
            payload["excluded_products_due_to_other_component_dur"] = excluded_products[
                :max_excluded_reason_items
            ]
            payload["other_component_dur_filtered_count"] = len(excluded_products)

            if excluded_products:
                if payload["products"]:
                    payload["other_component_dur_notice"] = (
                        f"추가 주성분 DUR 위험 제품 {len(excluded_products)}개를 제외하고 "
                        f"복용 가능 후보 {len(payload['products'])}개를 추천합니다."
                    )
                else:
                    payload["other_component_dur_notice"] = (
                        "후보 제품이 추가 주성분 DUR 위험으로 제외되었습니다."
                    )

            diagnostics = payload.get("diagnostics")
            if not isinstance(diagnostics, dict):
                diagnostics = {}
            diagnostics["other_component_dur_filtered"] = len(excluded_products)
            diagnostics["other_component_dur_kept"] = len(payload["products"])
            payload["diagnostics"] = diagnostics

    items = await asyncio.gather(*[fetch_one(ingr) for ingr in ingredients])
    extra_component_task = asyncio.create_task(attach_other_component_dur_guidance(items))

    raw_warning_map = {
        item["ingredient"]: item.get("us_warning_raw")
        for item in items
        if item.get("ingredient")
    }
    summarized_map = await AIService.bulk_summarize_fda_warnings(raw_warning_map)
    await extra_component_task

    for item in items:
        ingredient = item.get("ingredient")
        item["us_warning"] = summarized_map.get(ingredient) if ingredient else None
        item.pop("us_warning_raw", None)

    with_products = []
    empty_products = []
    for item in items:
        ingredient = str(item.get("ingredient") or "")
        products = item.get("products") or []
        if products:
            with_products.append(ingredient)
        else:
            empty_products.append(ingredient)

    logger.warning(
        "symptom_products_api summary: symptom='%s' requested=%d with_products=%d without_products=%d max_visible=3",
        symptom,
        len(ingredients),
        len(with_products),
        len(empty_products),
    )
    logger.warning(
        "symptom_products_api requested ingredients: %s",
        ", ".join(ingredients) if ingredients else "(none)",
    )
    logger.warning(
        "symptom_products_api ingredients with products: %s",
        ", ".join(with_products) if with_products else "(none)",
    )
    if empty_products:
        logger.warning(
            "symptom_products_api ingredients without products: %s",
            ", ".join(empty_products),
        )
    if debug_mode:
        diagnostics = [
            {
                "ingredient": item.get("ingredient"),
                "product_count": len(item.get("products") or []),
                "diagnostics": item.get("diagnostics", {}),
            }
            for item in items
        ]
        logger.warning(
            "symptom_products_api diagnostics: %s",
            json.dumps(diagnostics, ensure_ascii=False),
        )

    response_payload = {"status": "success", "items": items}
    if debug_mode:
        response_payload["diagnostics"] = [
            {
                "ingredient": item.get("ingredient"),
                "product_count": len(item.get("products") or []),
                "diagnostics": item.get("diagnostics", {}),
            }
            for item in items
        ]
    return JsonResponse(response_payload)
