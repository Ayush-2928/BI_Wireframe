import hashlib
import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
from jose import jwt

from database import get_db
from models import LoginRequest
from config import settings

router = APIRouter()

_SALT = "yuqta_salt_2025"


def _hash_password(password: str) -> str:
    return hashlib.sha256(f"{_SALT}{password}".encode()).hexdigest()


def _create_token(username: str) -> str:
    payload = {
        "sub": username,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=settings.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


@router.post("/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    hashed = _hash_password(req.password)

    row = db.execute(
        text(
            "SELECT username FROM pgadmin_appuser "
            "WHERE username = :username AND password_hash = :password_hash"
        ),
        {"username": req.username, "password_hash": hashed},
    ).fetchone()

    if not row:
        return {"success": False, "data": None, "error": "Invalid username or password"}

    token = _create_token(req.username)
    return {
        "success": True,
        "data": {"token": token, "username": req.username},
        "error": None,
    }
