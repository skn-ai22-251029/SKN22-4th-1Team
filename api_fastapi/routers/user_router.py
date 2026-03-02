from fastapi import APIRouter, Depends, Request, Form, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from services.auth_service import get_current_user_from_token
from services.user_service import UserService
import os

router = APIRouter(prefix="/user", tags=["user"])

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
templates = Jinja2Templates(directory=os.path.join(parent_dir, "templates"))

@router.get("/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return RedirectResponse(url="/auth/login")
    
    if token.startswith("Bearer "):
        token = token.split(" ")[1]

    try:
        user = await get_current_user_from_token(token)
        profile = await UserService.get_profile(user)
    except Exception:
         return RedirectResponse(url="/auth/login")

    return templates.TemplateResponse("profile.html", {
        "request": request, 
        "user": user,
        "profile": profile
    })

@router.post("/profile")
async def update_profile(
    request: Request,
    medications: str = Form(""),
    allergies: str = Form(""),
    diseases: str = Form("")
):
    token = request.cookies.get("access_token")
    if not token:
        return RedirectResponse(url="/auth/login")
    
    if token.startswith("Bearer "):
        token = token.split(" ")[1]

    try:
        user = await get_current_user_from_token(token)
        await UserService.update_profile(user, medications, allergies, diseases)
    except Exception:
         return RedirectResponse(url="/auth/login")
         
    return RedirectResponse(url="/user/profile", status_code=status.HTTP_302_FOUND)
