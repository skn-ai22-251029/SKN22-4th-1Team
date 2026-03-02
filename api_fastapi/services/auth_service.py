import os
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from django.contrib.auth.models import User
from django.contrib.auth import authenticate
from asgiref.sync import sync_to_async
from django.db import IntegrityError

# --- Configuration ---
SECRET_KEY = os.getenv("SECRET_KEY", "u2983y8923u8923u8923u8923u8923") # Fallback for dev
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")

class AuthService:
    @staticmethod
    def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
        to_encode = data.copy()
        if expires_delta:
            expire = datetime.utcnow() + expires_delta
        else:
            expire = datetime.utcnow() + timedelta(minutes=15)
        to_encode.update({"exp": expire})
        encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
        return encoded_jwt

    @staticmethod
    @sync_to_async
    def authenticate_user(username, password):
        user = authenticate(username=username, password=password)
        return user

    @staticmethod
    @sync_to_async
    def create_user(username, password, email=""):
        try:
            user = User.objects.create_user(username=username, password=password, email=email)
            return user
        except IntegrityError:
            return None

    @staticmethod
    @sync_to_async
    def get_user(username):
        try:
            return User.objects.get(username=username)
        except User.DoesNotExist:
            return None

async def get_current_user_from_token(token: str):
    """
    Decodes the JWT token and retrieves the user.
    This function is used for dependency injection in endpoints.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    user = await AuthService.get_user(username)
    if user is None:
        raise credentials_exception
    return user

async def get_current_user_optional(token: Optional[str] = None):
    """
    Returns user if token is valid, else None.
    Used for templates where login is optional.
    """
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            return None
        user = await AuthService.get_user(username)
        return user
    except JWTError:
        return None
