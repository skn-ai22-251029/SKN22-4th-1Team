import os
import sys
import django
import logging
from dotenv import load_dotenv

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 1. Django 초기화 및 환경변수 로드
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.append(os.path.join(project_root, 'backend_django'))

# .env 파일 명시적 로드
env_path = os.path.join(project_root, '.env')
load_dotenv(env_path)
logger.info(f"Loading .env from: {env_path}")

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings')
django.setup()

from fastapi import FastAPI, Request, HTTPException, APIRouter
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

# 2. 서비스 로드
from services.drug_service import DrugService
from services.ai_service import AIService
from services.auth_service import get_current_user_optional
from services.user_service import UserService
from services.map_service import MapService
from routers import auth_router, user_router, drug_router
# 3. LangGraph 로드
from graph_agent.builder import build_graph

app = FastAPI(title="Global Drug Safety Intelligence")
app.include_router(auth_router.router)
app.include_router(user_router.router)
app.include_router(drug_router.router)
templates = Jinja2Templates(directory=os.path.join(current_dir, "templates"))
router = APIRouter()

@app.on_event("startup")
async def startup_event():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("CRITICAL: OPENAI_API_KEY is missing! Chatbot features will not work.")
    else:
        logger.info(f"OPENAI_API_KEY loaded successfully. (Starts with: {api_key[:7]}...)")
    
    # LangSmith Config (Map custom key if needed)
    if os.getenv("LANGSMITH_API_KEY") and not os.getenv("LANGCHAIN_API_KEY"):
        os.environ["LANGCHAIN_API_KEY"] = os.getenv("LANGSMITH_API_KEY")
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        logger.info("Mapped LANGSMITH_API_KEY to LANGCHAIN_API_KEY and enabled tracing.")

    # Initialize LangGraph
    app.state.graph = build_graph()
    logger.info("LangGraph workflow initialized.")

@app.get("/", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    token = request.cookies.get("access_token")
    if token and token.startswith("Bearer "):
        token = token.split(" ")[1]
    user = await get_current_user_optional(token)
    return templates.TemplateResponse("index.html", {"request": request, "user": user})

@app.get("/web-search/{drug_name}", response_class=HTMLResponse)
async def product_search(request: Request, drug_name: str):
    """제품명 기반 검색 (웹 화면용)"""
    # 비동기 서비스 호출
    logger.info(f"Searching FDA for Product: {drug_name}")
    fda_result = await DrugService.search_fda(drug_name)
    
    if not fda_result:
        return templates.TemplateResponse("error.html", {
            "request": request, "message": f"'{drug_name}' 정보를 FDA에서 찾을 수 없습니다."
        })
    
    kr_dur = await DrugService.get_dur_by_ingr(fda_result['active_ingredients'])
    
    return templates.TemplateResponse("search_result.html", {
        "request": request, 
        "drug_name": drug_name, 
        "ingredients": fda_result['active_ingredients'],
        "us_guideline": fda_result, 
        "kr_dur": kr_dur, 
        "dur_count": len(kr_dur),
        "maps_key": os.getenv("GOOGLE_MAPS_API_KEY")
    })

@app.get("/smart-search", response_class=HTMLResponse)
async def smart_search(request: Request, q: str):
    """지능형 RAG 검색 (LangGraph 기반)"""
    if not q: return HTMLResponse("<script>alert('검색어를 입력하세요.'); history.back();</script>")

    logger.info(f"LangGraph User Query: {q}")

    # Run LangGraph Workflow
    user_profile_data = None
    try:
        token = request.cookies.get("access_token")
        if token and token.startswith("Bearer "):
            token = token.split(" ")[1]
        user = await get_current_user_optional(token)
        if user:
            profile = await UserService.get_profile(user)
            if profile:
                user_profile_data = {
                    "current_medications": profile.current_medications,
                    "allergies": profile.allergies,
                    "chronic_diseases": profile.chronic_diseases
                }
    except Exception as e:
        logger.error(f"Error fetching user profile: {e}")

    inputs = {"query": q, "user_profile": user_profile_data}
    try:
        result = await app.state.graph.ainvoke(inputs)
    except Exception as e:
        logger.error(f"Graph Execution Error: {e}")
        return templates.TemplateResponse("error.html", {
            "request": request, "message": f"처리 중 오류가 발생했습니다: {str(e)}"
        })

    category = result.get("category")
    final_answer = result.get("final_answer", "")
    
    logger.info(f"Graph Result Category: {category}")

    if category == "symptom_recommendation":
        return templates.TemplateResponse("symptom_result.html", {
            "request": request, 
            "symptom": q, 
            "answer": final_answer,
            "dur_details": result.get("dur_data", []),
            "maps_key": os.getenv("GOOGLE_MAPS_API_KEY")
        })

    elif category == "product_request":
        fda = result.get("fda_data")
        dur = result.get("dur_data", [])
        
        if not fda:
             return templates.TemplateResponse("error.html", {
                "request": request, "message": final_answer or f"'{q}' 관련 정보를 찾을 수 없습니다."
            })
            
        return templates.TemplateResponse("search_result.html", {
            "request": request,
            "drug_name": fda.get("brand_name", q),
            "ingredients": fda.get("active_ingredients"),
            "us_guideline": fda,
            "kr_dur": dur,
            "dur_count": len(dur),
            "maps_key": os.getenv("GOOGLE_MAPS_API_KEY")
        })
        
    elif category == "general_medical":
        # 일반 의학 질문은 증상 결과 페이지 형식을 빌려 텍스트 위주로 표시
        return templates.TemplateResponse("symptom_result.html", {
            "request": request,
            "symptom": q,
            "answer": final_answer,
            "dur_details": [], # DUR 정보 없음
            "maps_key": os.getenv("GOOGLE_MAPS_API_KEY")
        })
    
    else: # error or invalid
        return templates.TemplateResponse("error.html", {
            "request": request, "message": final_answer or "요청을 처리할 수 없습니다."
        })

# API 엔드포인트 (JSON 반환용)
@router.get("/global-search/{drug_name}")
async def global_drug_search(drug_name: str):
    fda_result = await DrugService.search_fda(drug_name)
    if not fda_result:
        raise HTTPException(status_code=404, detail="FDA info not found")
    
    kr_dur_result = await DrugService.get_dur_by_ingr(fda_result['active_ingredients'])
    
    return {
        "status": "success",
        "origin": "USA",
        "drug_identity": {
            "name": fda_result['brand_name'],
            "ingredients": fda_result['active_ingredients']
        },
        "us_guideline": {
            "purpose": fda_result['indications'],
            "fda_warnings": fda_result['warnings']
        },
        "kr_safety_standard": {
            "dur_count": len(kr_dur_result),
            "dur_details": kr_dur_result
        }
    }

@app.get("/api/pharmacies")
async def get_nearby_pharmacies(lat: float, lng: float):
    try:
        results = await MapService.find_nearby_pharmacies(lat, lng)
        return {"status": "success", "results": results, "api_key_loaded": bool(os.getenv("GOOGLE_MAPS_API_KEY"))}
    except Exception as e:
        import traceback
        return {"status": "error", "message": str(e), "traceback": traceback.format_exc(), "api_key_loaded": bool(os.getenv("GOOGLE_MAPS_API_KEY"))}

app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)