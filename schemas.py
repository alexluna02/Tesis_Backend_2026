from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


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
    especialidad: Optional[str] = None

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
    # Solo si se envía: la especialidad que el experto muestra a los agricultores
    especialidad: Optional[str] = Field(None, max_length=80, example="Fitopatología del maíz")


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


AssignableRole = Literal["agricultor", "investigador", "experto"]


class AdminUserCreate(UserCreate):
    rol: AssignableRole = "agricultor"


class AdminUserUpdate(BaseModel):
    rol: Optional[AssignableRole] = None
    activo: Optional[bool] = None


# ── Consultas a expertos ──────────────────────────────────────────────────────
# Clases que devuelve el modelo (incluye las variantes antiguas de algunos registros)
ClaseModelo = Literal["Fondo", "Fondo_y_Sana", "Tizon", "Roya", "Mancha_blanca", "Mancha_Blanca"]
DiagnosticoExperto = Literal["Fondo", "Tizon", "Roya", "Mancha_blanca", "Otro"]


def _utc_naive(value: datetime) -> datetime:
    """Fechas con zona horaria → UTC sin zona (la convención de toda la base de datos)."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class PuntoGeo(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)


class MuestraParcela(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    etiqueta: str = Field(..., min_length=1, max_length=10)
    lat: float = Field(..., ge=-90, le=90)
    lng: float = Field(..., ge=-180, le=180)
    clase: Optional[ClaseModelo] = None          # null = zona sin analizar
    afectacion_pct: Optional[float] = Field(None, ge=0, le=100)


class ParcelaSnapshot(BaseModel):
    """Foto de una visita de parcela (los análisis por parcela viven en el navegador)."""
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    parcela_nombre: str = Field(..., min_length=1, max_length=120)
    hectareas: float = Field(..., ge=0, le=100000)
    fecha: datetime
    vertices: List[PuntoGeo] = Field(..., min_length=3, max_length=200)
    muestras: List[MuestraParcela] = Field(..., min_length=1, max_length=100)
    afectacion_promedio: float = Field(..., ge=0, le=100)
    zonas_afectadas: int = Field(..., ge=0, le=100)
    zonas_total: int = Field(..., ge=0, le=100)

    _fecha_utc = field_validator("fecha")(_utc_naive)


class ConsultaCreate(BaseModel):
    experto_id: UUID
    tipo: Literal["individual", "parcela", "general"]       # general = chat libre con el experto
    analisis_id: Optional[UUID] = None                     # obligatorio si tipo = individual
    referencia: Optional[str] = Field(None, min_length=1, max_length=100)
    parcela: Optional[ParcelaSnapshot] = None              # obligatorio si tipo = parcela
    mensaje: Optional[str] = Field(None, max_length=4000)  # primera pregunta, opcional
    client_id: Optional[str] = Field(None, min_length=1, max_length=64)


class MensajeCreate(BaseModel):
    texto: Optional[str] = Field(None, max_length=4000)
    imagen_url: Optional[str] = Field(None, max_length=2_800_000)  # data URL jpeg/png/webp ≤ 2 MB
    client_id: Optional[str] = Field(None, min_length=1, max_length=64)


class DictamenUpdate(BaseModel):
    coincide: bool
    diagnostico: DiagnosticoExperto
    texto: str = Field(..., min_length=1, max_length=4000)


class EstadoConsultaUpdate(BaseModel):
    estado: Literal["abierta", "cerrada"]


class ExpertoRead(BaseModel):
    id: UUID
    nombre: str
    consultas_abiertas: int
    especialidad: Optional[str] = None
    en_linea: bool = False                           # con actividad en los últimos 90 s
    ultima_actividad: Optional[datetime] = None


class ParticipanteRead(BaseModel):
    id: UUID
    nombre: str
    rol: str
    especialidad: Optional[str] = None
    en_linea: bool = False
    ultima_actividad: Optional[datetime] = None


class MensajeRead(BaseModel):
    id: UUID
    consulta_id: UUID
    autor_id: UUID
    texto: Optional[str] = None
    imagen_url: Optional[str] = None
    documento_nombre: Optional[str] = None           # PDF adjunto: se descarga aparte
    documento_tamano: Optional[int] = None           # bytes
    client_id: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class UltimoMensajeRead(BaseModel):
    texto: Optional[str] = None      # recortado a 140 caracteres
    tiene_imagen: bool
    documento_nombre: Optional[str] = None
    autor_id: UUID
    created_at: datetime


class DictamenRead(BaseModel):
    coincide: bool
    diagnostico: str
    texto: str
    fecha: datetime


class ConsultaItem(BaseModel):
    id: UUID
    tipo: str
    estado: str
    created_at: datetime
    ultimo_mensaje_at: datetime
    rol_en_consulta: Literal["solicitante", "experto"]
    contraparte: ParticipanteRead
    resumen: dict                    # datos clave del análisis (versión corta)
    ultimo_mensaje: Optional[UltimoMensajeRead] = None
    no_leidos: int
    nueva: bool                      # soy el experto y aún no la abrí
    dictamen: Optional[DictamenRead] = None
    dictamen_nuevo: bool             # soy el solicitante y hay un dictamen que no he visto


class AnalisisConsultaRead(BaseModel):
    id: UUID
    imagen_url: str
    grad_cam_url: Optional[str] = None
    resultado_url: Optional[str] = None
    fecha: datetime
    afectacion_pct: Decimal
    severidad: str
    clase: str
    clase_idx: int


class ConsultaDetalle(ConsultaItem):
    solicitante: ParticipanteRead
    experto: ParticipanteRead
    snapshot: dict                   # foto completa del análisis al enviarlo
    analisis: Optional[AnalisisConsultaRead] = None   # imágenes, si el análisis individual sigue existiendo
    leido_contraparte_at: Optional[datetime] = None   # para el doble check de «leído»
    puede_escribir: bool
    soy_experto: bool
    cerrada_at: Optional[datetime] = None
    ya_existia: bool = False         # POST /consultas devolvió una consulta abierta que ya existía


class MensajesRespuesta(BaseModel):
    mensajes: List[MensajeRead]
    estado: str
    leido_contraparte_at: Optional[datetime] = None
    dictamen_fecha: Optional[datetime] = None
    ultimo_mensaje_at: datetime


class ConsultasResumen(BaseModel):
    no_leidos: int                   # mensajes sin leer en todas mis consultas
    consultas_con_novedad: int       # consultas con mensajes nuevos, sin abrir o con dictamen nuevo
    nuevas: int                      # (experto) consultas recibidas que aún no abrió
