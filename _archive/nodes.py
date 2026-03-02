import logging
from .state import AgentState
from services.ai_service import AIService
from services.drug_service import DrugService

logger = logging.getLogger(__name__)

async def classify_node(state: AgentState) -> AgentState:
    """Classify user query and extract keywords"""
    query = state["query"]
    intent = await AIService.classify_intent(query)
    
    category = intent.get("category", "invalid")
    keyword = intent.get("keyword", "")
    
    # Validation logic
    if category == "symptom_recommendation":
        symptom = query
    else:
        symptom = None
        
    return {
        "category": category,
        "keyword": keyword,
        "symptom": symptom
    }

async def retrieve_fda_node(state: AgentState) -> AgentState:
    """Retrieve FDA data based on category"""
    category = state["category"]
    keyword = state["keyword"]
    query = state["query"]
    
    fda_data = None
    
    if category == "symptom_recommendation":
        # Symptom search logic
        eng_kw = [keyword] if keyword and keyword != "none" else ["pain"]
        fda_ingrs = await DrugService.get_ingrs_from_fda_by_symptoms(eng_kw)
        
        # [Agentic Fallback]
        if not fda_ingrs:
            logger.info(f"FDA search failed for '{keyword}'. Requesting AI symptom synonyms.")
            synonyms = await AIService.get_symptom_synonyms(keyword or query)
            if synonyms:
                logger.info(f"AI suggested synonyms: {synonyms}. Retrying FDA search.")
                print(f"🔄 AI가 '{keyword}' 대신 검색해볼 유사 증상 제안: {synonyms}")
                fda_ingrs = await DrugService.get_ingrs_from_fda_by_symptoms(synonyms)
            
            # FDA 재검색도 실패하면 그때 성분 추천으로 폴백
            if not fda_ingrs:
                logger.info(f"FDA search with synonyms failed. Requesting AI ingredient recommendation.")
                fda_ingrs = await AIService.recommend_ingredients_for_symptom(keyword or query)
                logger.info(f"AI recommended ingredients: {fda_ingrs}")
                print(f"✨ AI가 추천한 증상({keyword or query}) 대체 성분: {fda_ingrs}")
            
        fda_data = fda_ingrs # Store ingredients list
        
    elif category == "product_request":
        # Product search logic
        target = keyword if keyword and keyword != "none" else query
        fda_result = await DrugService.search_fda(target)
        fda_data = fda_result # Store full dict
        
    return {"fda_data": fda_data}

async def retrieve_dur_node(state: AgentState) -> AgentState:
    """Retrieve DUR data based on FDA ingredients"""
    category = state["category"]
    fda_data = state["fda_data"]
    
    dur_data = []
    
    if not fda_data:
        return {"dur_data": []}
        
    if category == "symptom_recommendation":
        # fda_data is list of ingredients
        if isinstance(fda_data, list):
            dur_data = await DrugService.get_enriched_dur_info(fda_data)
            
    elif category == "product_request":
        # fda_data is dict with 'active_ingredients'
        if isinstance(fda_data, dict):
            ingrs = fda_data.get('active_ingredients', '')
            dur_data = await DrugService.get_dur_by_ingr(ingrs)
            
    return {"dur_data": dur_data}

async def generate_symptom_answer_node(state: AgentState) -> AgentState:
    """Generate answer for symptom queries"""
    symptom = state["symptom"]
    dur_data = state["dur_data"]
    
    if not dur_data:
        # Fallback to general AI answer if DB search yields no results
        # This handles cases like "What medicine for cold?" where DB might not match but AI knows general info.
        fallback_query = f"The user asked about '{symptom}' but I couldn't find specific drugs in the FDA/DUR database. Please provide general medical advice or common over-the-counter ingredients for this symptom. (User query: {state['query']})"
        answer = await AIService.generate_general_answer(fallback_query)
        prefix = "해당 증상에 대한 FDA/DUR 기반의 정확한 의약품 정보는 찾을 수 없었지만, 일반적인 정보를 안내해 드립니다.\n\n"
        return {"final_answer": prefix + answer}
        
    # AI 답변 생성을 위한 요약 데이터 생성
    summary_for_ai = []
    for item in dur_data:
        summary = f"Ingredient: {item['ingredient']}\n"
        summary += f"FDA Warning: {item['fda_warning'] if item['fda_warning'] else 'None'}\n"
        kr_warnings = [f"{d['type']}: {d['warning']}" for d in item['kr_durs']]
        summary += f"KR DUR: {', '.join(kr_warnings)}"
        summary_for_ai.append(summary)
    
    answer = await AIService.generate_symptom_answer(symptom, "\n---\n".join(summary_for_ai), state.get("user_profile"))
    return {"final_answer": answer}

async def generate_product_answer_node(state: AgentState) -> AgentState:
    """Generate answer for product queries (simple format for now, or use AI?)"""
    # The original product_search returned HTML directly. 
    # For LangGraph, we might want to return text or struct. 
    # Let's emulate the product search response or use a specific AI generator if needed.
    # For now, let's format the data nicely or just return the structured data to be rendered by template?
    # The user asked for an "Agent" to answer. 
    
    # Original logic just rendered `search_result.html`. 
    # To keep it "Agentic", maybe we should generate a summary text?
    # Or just pass the data through to be rendered.
    # But the user asked for "Agent to answer". 
    # Let's simply format the result into a string for the "answer".
    
    fda_data = state["fda_data"]
    dur_data = state["dur_data"]
    
    if not fda_data:
        return {"final_answer": "해당 의약품 정보를 찾을 수 없습니다."}
        
    # Construct a text response (since main.py might expect text if using graph for everything?)
    # or main.py will inspect the state and render HTML?
    # Use Plan: main.py will call graph and get final_answer. 
    # If we want to keep the rich UI, main.py might need to use intermediate state (fda_data, dur_data).
    
    # Let's generate a text summary for the "Agent" part.
    brand_name = fda_data.get('brand_name')
    indications = fda_data.get('indications')
    
    answer = f"**{brand_name}** 정보입니다.\n\n**효능/효과**:\n{indications}\n\n**DUR/주의사항**:\n"
    for d in dur_data:
        answer += f"- {d['ingr_name']} ({d['type']}): {d['warning_msg']}\n"
        
    return {"final_answer": answer}

async def generate_general_answer_node(state: AgentState) -> AgentState:
    """Generate answer for general medical queries"""
    query = state["query"]
    answer = await AIService.generate_general_answer(query)
    return {"final_answer": answer}

async def generate_error_node(state: AgentState) -> AgentState:
    """Handle invalid queries"""
    return {"final_answer": "죄송합니다. 질문을 이해하지 못하거나 의약품과 관련이 없는 질문입니다."}
