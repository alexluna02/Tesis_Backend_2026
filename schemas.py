from datetime import datetime
from decimal import Decimal
from typing import List, Literal, Optional
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


class ResetCodeRequest(BaseModel):
    email: EmailStr
    code: str = Field(..., pattern=r"^\d{6}$", example="482913")


class ResetPasswordRequest(ResetCodeRequest):
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
    resultado_url: Optional[str] = None
    afectacion_pct: Decimal
    severidad: str = Field(..., example="moderada")
    enfermedad_id: int


class AnalisisRead(BaseModel):
    id: UUID
    imagen_url: str
    grad_cam_url: Optional[str] = None
    resultado_url: Optional[str] = None
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


class PrecioPunto(BaseModel):
    fecha: datetime
    precio: Decimal
    precio_min: Optional[Decimal] = None
    precio_max: Optional[Decimal] = None
    presentacion: Optional[str] = None

    class Config:
        from_attributes = True


class ChoclosResumen(BaseModel):
    precio: Decimal
    precio_min: Optional[Decimal] = None
    precio_max: Optional[Decimal] = None
    unidad: Optional[str] = None
    fecha: datetime
    fuente: str
    variedades: Optional[str] = None
    tendencia: str  # 'sube' | 'baja' | 'estable'
    variacion_abs: Optional[Decimal] = None
    variacion_pct: Optional[Decimal] = None


class MaizDuroResumen(BaseModel):
    precio_promedio: Decimal
    precio_min: Decimal
    precio_max: Decimal
    unidad: str
    fuente: str
    temporada: str
    actualizado: datetime


class MercadoResumen(BaseModel):
    choclo: Optional[ChoclosResumen] = None
    maiz_duro: Optional[MaizDuroResumen] = None


class SeriePrecio(BaseModel):
    fecha: datetime
    precio: Decimal
    precio_min: Optional[Decimal] = None
    precio_max: Optional[Decimal] = None


class SeriePunto(BaseModel):
    fecha: datetime
    precio: Decimal


class VariedadMercado(BaseModel):
    nombre: str
    precio: Decimal
    fecha: datetime
    precio_semana_anterior: Optional[Decimal] = None
    fecha_semana_anterior: Optional[datetime] = None
    variacion_semanal: Optional[Decimal] = None
    variacion_semanal_pct: Optional[Decimal] = None
    tendencia: str
    serie: List[SeriePunto] = []


class ProductoMercado(BaseModel):
    producto: str
    nombre: str
    unidad: Optional[str] = None
    precio: Decimal
    precio_min: Optional[Decimal] = None
    precio_max: Optional[Decimal] = None
    fecha: datetime
    variedades: Optional[str] = None
    precio_semana_anterior: Optional[Decimal] = None
    fecha_semana_anterior: Optional[datetime] = None
    variacion_semanal: Optional[Decimal] = None
    variacion_semanal_pct: Optional[Decimal] = None
    tendencia: str  # 'sube' | 'baja' | 'estable' (frente a la semana anterior)
    serie: List[SeriePrecio] = []
    variedades_detalle: List[VariedadMercado] = []  # cada variedad por separado (si hay 2 o más)


class MercadoProductos(BaseModel):
    fuente: str
    fuente_url: str
    actualizado: Optional[datetime] = None
    productos: List[ProductoMercado] = []
    maiz_duro: Optional[MaizDuroResumen] = None


AssignableRole = Literal["agricultor", "investigador"]


class AdminUserCreate(UserCreate):
    rol: AssignableRole = "agricultor"


class AdminUserUpdate(BaseModel):
    rol: Optional[AssignableRole] = None
    activo: Optional[bool] = None
