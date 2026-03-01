import os
import re
import logging
import asyncio
from supabase import create_client, Client

from services.ai_service_v2 import AIService
from services.ingredient_utils import canonicalize_ingredient_name

logger = logging.getLogger(__name__)


class SupabaseService:
    _client = None

    @staticmethod
    async def _run_io(func):
        """Run blocking Supabase SDK calls in a worker thread."""
        return await asyncio.to_thread(func)

    @classmethod
    def get_client(cls) -> Client:
        if cls._client:
            return cls._client

        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")

        if not url or not key:
            logger.error("SUPABASE_URL and SUPABASE_KEY must be set in .env")
            return None

        cls._client = create_client(url, key)
        return cls._client

    @classmethod
    async def auth_sign_up(cls, email, password):
        client = cls.get_client()
        try:
            response = await cls._run_io(
                lambda: client.auth.sign_up({"email": email, "password": password})
            )
            return response.user, None
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[Supabase Auth] Sign up error: {error_msg}")
            if "already registered" in error_msg.lower():
                return None, "exists"
            return None, error_msg

    @classmethod
    async def auth_sign_in(cls, email, password):
        client = cls.get_client()
        try:
            response = await cls._run_io(
                lambda: client.auth.sign_in_with_password(
                    {"email": email, "password": password}
                )
            )
            return response.user, response.session
        except Exception as e:
            logger.error(f"[Supabase Auth] Sign in error: {e}")
            return None, None

    @classmethod
    async def auth_update_password(cls, new_password):
        client = cls.get_client()
        try:
            response = await cls._run_io(
                lambda: client.auth.update_user({"password": new_password})
            )
            return response.user, None
        except Exception as e:
            logger.error(f"[Supabase Auth] Password update error: {e}")
            return None, str(e)

    @classmethod
    async def auth_delete_user(cls, user_id: str):
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        if not url or not key:
            return False, "Supabase credentials are not configured."
        try:
            # Use a fresh client to avoid stale auth state after admin operations.
            fresh_client = await cls._run_io(lambda: create_client(url, key))
            await cls._run_io(lambda: fresh_client.auth.admin.delete_user(str(user_id)))
            cls._client = None
            return True, None
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[Supabase Auth] Account delete error for {user_id}: {error_msg}")
            return False, error_msg

    @classmethod
    async def get_dur_by_ingr(cls, ingr_text: str):
        if not ingr_text:
            return []

        ingr_list = [
            i.strip() for i in ingr_text.replace(",", "/").split("/") if len(i.strip()) > 1
        ]
        dur_data = await cls._get_dur_data_from_supabase(ingr_list)

        results = []
        for d in dur_data:
            dur_type = d.get("dur_type")
            type_name = d.get("type_name") or dur_type
            mixture_ingr = (d.get("mixture_ingr_eng_name") or "").strip()
            warning_msg = d.get("prohbt_content") or d.get("remark")
            if dur_type == "COMBINED" and mixture_ingr:
                warning_msg = f"병용금기 성분: {mixture_ingr}"

            results.append(
                {
                    "type": type_name,
                    "ingr_name": d.get("ingr_kor_name"),
                    "warning_msg": warning_msg,
                    "severity": d.get("critical_value"),
                }
            )
        return results

    @classmethod
    async def get_enriched_dur_info(cls, ingr_list: list):
        unique_ingrs = sorted(list(set([i.upper() for i in ingr_list])))
        enriched_data = []

        from services.drug_service import DrugService as OriginalDrugService

        for ingr in unique_ingrs:
            durs = await cls._get_kr_durs_supabase(ingr)
            fda_warn = await OriginalDrugService.get_fda_warnings_by_ingr(ingr)
            if fda_warn:
                summary = await AIService.summarize_fda_warning(fda_warn)
                if summary:
                    fda_warn = summary

            enriched_data.append(
                {
                    "ingredient": ingr,
                    "kr_durs": durs,
                    "fda_warning": fda_warn,
                }
            )
        return enriched_data

    @classmethod
    async def _get_kr_durs_supabase(cls, ingr_name):
        if not ingr_name:
            return []

        target_name = canonicalize_ingredient_name(ingr_name.strip()) or ingr_name.strip()
        if not target_name:
            return []

        client = cls.get_client()
        if not client:
            return []

        dur_list = []
        try:
            # 1) prefix match first (faster/saner), 2) broad contains fallback
            response = await cls._run_io(
                lambda: (
                    client.table("dur_master")
                    .select(
                        "dur_type,type_name,ingr_kor_name,prohbt_content,remark,mixture_ingr_eng_name"
                    )
                    .ilike("ingr_eng_name", f"{target_name.lower()}%")
                    .execute()
                )
            )
            dur_list = response.data or []

            if not dur_list:
                response = await cls._run_io(
                    lambda: (
                        client.table("dur_master")
                        .select(
                            "dur_type,type_name,ingr_kor_name,prohbt_content,remark,mixture_ingr_eng_name"
                        )
                        .ilike("ingr_eng_name", f"%{target_name.lower()}%")
                        .execute()
                    )
                )
                dur_list = response.data or []
        except Exception as e:
            logger.error(f"[Supabase] DUR query error for '{target_name}': {e}")
            return []

        grouped_results = {}
        for d in dur_list:
            dur_type = d.get("dur_type")
            type_name = str(d.get("type_name") or dur_type or "").strip()
            if not type_name:
                continue

            mixture_ingr = (d.get("mixture_ingr_eng_name") or "").strip()
            if dur_type == "COMBINED" and mixture_ingr:
                content = f"병용금기 성분: {mixture_ingr}"
            else:
                content = f"{type_name} 금기 사항이 있을 수 있습니다. 의사/약사와 상담 후 복용하세요."

            if type_name not in grouped_results:
                grouped_results[type_name] = {
                    "type": type_name,
                    "kor_name": d.get("ingr_kor_name"),
                    "warnings": set(),
                }
            grouped_results[type_name]["warnings"].add(content)

        results = []
        for val in grouped_results.values():
            combined_warning = "\n".join(sorted(list(val["warnings"])))
            results.append(
                {
                    "type": val["type"],
                    "kor_name": val["kor_name"],
                    "warning": combined_warning,
                }
            )
        return results

    @classmethod
    async def _get_dur_data_from_supabase(cls, ingr_list: list):
        client = cls.get_client()
        if not client:
            return []

        all_results = []
        for ingr in ingr_list:
            if not ingr:
                continue
            target = canonicalize_ingredient_name(ingr.strip()) or ingr.strip()
            try:
                # 1) prefix match first
                response = await cls._run_io(
                    lambda: (
                        client.table("dur_master")
                        .select(
                            "dur_type,type_name,ingr_kor_name,critical_value,prohbt_content,remark,mixture_ingr_eng_name"
                        )
                        .ilike("ingr_eng_name", f"{target.lower()}%")
                        .execute()
                    )
                )
                rows = response.data or []

                # 2) contains fallback
                if not rows:
                    response = await cls._run_io(
                        lambda: (
                            client.table("dur_master")
                            .select(
                                "dur_type,type_name,ingr_kor_name,critical_value,prohbt_content,remark,mixture_ingr_eng_name"
                            )
                            .ilike("ingr_eng_name", f"%{target.lower()}%")
                            .execute()
                        )
                    )
                    rows = response.data or []

                if rows:
                    all_results.extend(rows)
            except Exception as e:
                logger.error(f"[Supabase] Batch DUR query error for '{target}': {e}")

        return all_results

    @classmethod
    async def get_symptom_cache(cls, query_text: str):
        client = cls.get_client()
        if not client:
            return None

        try:
            response = await cls._run_io(
                lambda: (
                client.table("search_cache")
                .select("*")
                .eq("query_text", query_text)
                .limit(1)
                .execute()
                )
            )
            if response.data:
                return response.data[0]
        except Exception as e:
            logger.error(f"[Cache] Error reading cache for '{query_text}': {e}")
        return None

    @classmethod
    async def set_symptom_cache(
        cls,
        query_text: str,
        category: str,
        fda_data: list,
        dur_data: list,
        final_answer: str,
        recommended_ingredients: list,
    ):
        client = cls.get_client()
        if not client:
            return False

        try:
            payload = {
                "query_text": query_text,
                "category": category,
                "fda_data": fda_data if fda_data else [],
                "dur_data": dur_data if dur_data else [],
                "final_answer": final_answer,
                "recommended_ingredients": (
                    recommended_ingredients if recommended_ingredients else []
                ),
            }
            await cls._run_io(
                lambda: client.table("search_cache")
                .upsert(payload, on_conflict="query_text")
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"[Cache] Error saving cache for '{query_text}': {e}")
            return False

    @classmethod
    async def search_ingredient_scores_by_symptom(
        cls,
        keyword: str,
        raw_query: str = "",
        max_rows: int = 5000,
        batch_size: int = 1000,
    ):
        """
        Return ranked ingredient scores from unified_drug_info rows matched by efficacy.
        Each score represents how frequently the ingredient appears in matched rows,
        which is used as a direct relevance proxy for the symptom.
        """
        client = cls.get_client()
        if not client:
            return []

        symptom_term = (keyword or "").strip()
        if not symptom_term and raw_query:
            symptom_term = raw_query.strip()
        if not symptom_term:
            return []

        rows = []
        start = 0
        while start < max_rows:
            end = min(start + batch_size - 1, max_rows - 1)
            try:
                response = await cls._run_io(
                    lambda: (
                    client.table("unified_drug_info")
                    .select("main_ingr_eng, efficacy")
                    .ilike("efficacy", f"%{symptom_term}%")
                    .range(start, end)
                    .execute()
                    )
                )
                batch = response.data or []
            except Exception as e:
                logger.warning(
                    f"[Supabase] Symptom efficacy query failed for term '{symptom_term}': {e}"
                )
                return []

            if not batch:
                break

            rows.extend(batch)
            if len(batch) < (end - start + 1):
                break
            start += batch_size

        def extract_ingredient_tokens(text: str):
            if not text:
                return []

            cleaned = re.sub(r"\([^)]*\)", " ", str(text))
            cleaned = re.sub(
                r"\b\d+(\.\d+)?\s*(mg|mcg|g|ml|%)\b", " ", cleaned, flags=re.I
            )
            parts = re.split(r"[,;/+\n]| and | AND ", cleaned)

            tokens = []
            for part in parts:
                token = re.sub(r"\s{2,}", " ", part).strip(" .:-_")
                if len(token) < 3:
                    continue
                tokens.append(token)
            return tokens

        scores = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            for token in extract_ingredient_tokens(row.get("main_ingr_eng")):
                if not re.search(r"[A-Za-z]", token):
                    continue
                normalized = canonicalize_ingredient_name(token)
                if not normalized:
                    continue
                scores[normalized] = scores.get(normalized, 0) + 1

        if not scores:
            return []

        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
        return [{"ingredient": name, "score": score} for name, score in ranked]

    @classmethod
    async def search_ingredients_by_symptom(
        cls,
        keyword: str,
        raw_query: str = "",
        top_n: int = 8,
        max_rows: int = 5000,
        limit: int = None,
    ):
        if isinstance(limit, int) and limit > 0:
            max_rows = limit

        ranked = await cls.search_ingredient_scores_by_symptom(
            keyword=keyword,
            raw_query=raw_query,
            max_rows=max_rows,
        )
        if not ranked:
            return []

        ingredients = [item["ingredient"] for item in ranked if item.get("ingredient")]
        if top_n is None or top_n <= 0:
            return ingredients
        return ingredients[:top_n]

    @classmethod
    async def search_drugs(cls, query_text: str, limit: int = 20):
        client = cls.get_client()
        if not client:
            return []

        try:
            response = await cls._run_io(
                lambda: (
                client.table("unified_drug_info")
                .select("item_name, entp_name")
                .or_(f"item_name.ilike.%{query_text}%,entp_name.ilike.%{query_text}%")
                .limit(limit)
                .execute()
                )
            )
            return response.data
        except Exception as e:
            logger.error(f"[Supabase] Drug search error: {e}")
            return []

    @classmethod
    async def get_user_profile(cls, user_id: str):
        client = cls.get_client()
        if not client:
            return None

        try:
            response = await cls._run_io(
                lambda: (
                client.table("user_profile")
                .select("*")
                .eq("user_id", user_id)
                .limit(1)
                .execute()
                )
            )
            if response.data:
                return response.data[0]
        except Exception as e:
            logger.error(f"[Supabase] Profile fetch error for user {user_id}: {e}")
        return None

    @classmethod
    async def update_user_profile(
        cls,
        user_id: str,
        current_medications: str,
        allergies: str,
        chronic_diseases: str,
        is_pregnant: bool = False,
    ):
        client = cls.get_client()
        if not client:
            return None

        try:
            payload = {
                "user_id": str(user_id),
                "current_medications": current_medications,
                "allergies": allergies,
                "chronic_diseases": chronic_diseases,
                "is_pregnant": is_pregnant,
            }
            response = await cls._run_io(
                lambda: client.table("user_profile")
                .upsert(payload, on_conflict="user_id")
                .execute()
            )
            return response.data[0] if response.data else None
        except Exception as e:
            logger.error(f"[Supabase] Profile update error for user {user_id}: {e}")
            return None

    @classmethod
    async def delete_user_profile(cls, user_id: str):
        client = cls.get_client()
        if not client:
            return False

        try:
            await cls._run_io(
                lambda: client.table("user_profile")
                .delete()
                .eq("user_id", str(user_id))
                .execute()
            )
            return True
        except Exception as e:
            logger.error(f"[Supabase] Profile delete error for user {user_id}: {e}")
            return False
