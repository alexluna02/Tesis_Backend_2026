from datetime import datetime
from decimal import Decimal
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class UserBase(BaseModel):
    nombre: str = Field(..., example="María Pérez")
    email: EmailStr = Field(..., example="maria@ejemplo.com")


class UserCreate(UserBase):
    password: str = Field(..., min_length=8, example="unaClaveSegura123")


class UserRead(UserBase):
    id: UUID
    rol: str
    activo: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    sub: UUID
    email: EmailStr
    exp: int


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class GoogleLoginRequest(BaseModel):
    access_token: str


class UserUpdate(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=150, example="María Pérez")


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(..., min_length=8, example="nuevaClave123")


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    reset_token: str
    code: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=8)


class EnfermedadRead(BaseModel):
    id: int
    nombre: str
    clase_idx: int
    descripcion: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class AnalisisCreate(BaseModel):
    imagen_url: str
    grad_cam_url: Optional[str] = None
    afectacion_pct: Decimal
    severidad: str = Field(..., example="moderada")
    enfermedad_id: int


class AnalisisRead(BaseModel):
    id: UUID
    imagen_url: str
    grad_cam_url: Optional[str] = None
    afectacion_pct: Decimal
    severidad: str
    fecha: datetime
    enfermedad: EnfermedadRead

    class Config:
        from_attributes = True


class StatsResponse(BaseModel):
    total_analisis: int
    detecciones_enfermedad: int
    cultivos_sanos: int
    afectacion_promedio: Optional[Decimal] = None
