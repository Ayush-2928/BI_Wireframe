from pydantic import BaseModel
from typing import Optional, Any


# --- Envelope ---
class APIResponse(BaseModel):
    success: bool
    data: Optional[Any] = None
    error: Optional[str] = None


# --- Auth ---
class LoginRequest(BaseModel):
    username: str
    password: str


class TokenData(BaseModel):
    token: str
    username: str
