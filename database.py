import os
from typing import AsyncGenerator

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import (Boolean, CheckConstraint, Column, DateTime, ForeignKey, Index, Integer, SmallInteger,
                        String, Text, UniqueConstraint, func, text, Numeric)
from sqlalchemy.dialects.postgresql import JSONB, UUID, ENUM
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DATABASE_NAME = os.getenv("DATABASE_NAME", "bd_tesis")
DATABASE_USER = os.getenv("DATABASE_USER", "postgres")
DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD", "postgres")
DATABASE_HOST = os.getenv("DATABASE_HOST", "localhost")
DATABASE_PORT = os.getenv("DATABASE_PORT", "5432")

_raw_url = os.getenv(
    "DATABASE_URL",
    f"postgresql+asyncpg://{DATABASE_USER}:{DATABASE_PASSWORD}@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}",
)
DATABASE_URL = _raw_url.strip().split("\n")[0].split("\r")[0].strip()

# Render (and many cloud providers) supply postgresql:// or postgres://
# but SQLAlchemy async needs the postgresql+asyncpg:// dialect prefix.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(
    DATABASE_URL,
    future=True,
    echo=False,
    connect_args={"statement_cache_size": 0},
)
async_session = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

rol_usuario_enum = ENUM(
    "agricultor",
    "investigador",
    "director",
    "admin",
    "experto",
    name="rol_usuario",
    create_type=False,
)

nivel_severidad_enum = ENUM(
    "leve",
    "moderada",
    "grave",
    name="nivel_severidad",
    create_type=False,
)


class Base(DeclarativeBase):
    pass


class Enfermedad(Base):
    __tablename__ = "enfermedades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nombre: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    clase_idx: Mapped[int] = mapped_column(SmallInteger, nullable=False, unique=True)
    descripcion: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=False), server_default=func.now())


class Usuario(Base):
    __tablename__ = "usuarios"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )
    nombre: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    rol: Mapped[str] = mapped_column(
        rol_usuario_enum,
        nullable=False,
        server_default=text("'agricultor'::rol_usuario"),
    )
    activo: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("TRUE"))
    created_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False, server_default=func.now())
    updated_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False, server_default=func.now())

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        "RefreshToken", back_populates="user", cascade="all, delete-orphan"
    )
    analisis: Mapped[list["Analisis"]] = relationship(
        "Analisis", back_populates="user", cascade="all, delete-orphan"
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False)
    token: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False)
    revocado: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    created_at: Mapped[str] = mapped_column(DateTime(timezone=False), server_default=func.now())

    user: Mapped[Usuario] = relationship("Usuario", back_populates="refresh_tokens")


class PasswordReset(Base):
    """Código de recuperación de contraseña.

    Solo se guarda un HMAC del código: el código en claro viaja únicamente por correo
    y nunca se devuelve al navegador.
    """
    __tablename__ = "password_resets"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False)
    intentos: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    usado: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("FALSE"))
    created_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False, server_default=func.now())


class Analisis(Base):
    __tablename__ = "analisis"

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False)
    enfermedad_id: Mapped[int] = mapped_column(ForeignKey("enfermedades.id"), nullable=False)
    imagen_url: Mapped[str] = mapped_column(Text, nullable=False)
    grad_cam_url: Mapped[str] = mapped_column(Text, nullable=True)
    # Imagen de salida tal como se mostró al analizar (original + área afectada + caja + etiqueta)
    resultado_url: Mapped[str] = mapped_column(Text, nullable=True)
    afectacion_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    severidad: Mapped[str] = mapped_column(nivel_severidad_enum, nullable=False)
    fecha: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False, server_default=func.now())

    user: Mapped[Usuario] = relationship("Usuario", back_populates="analisis")
    enfermedad: Mapped[Enfermedad] = relationship("Enfermedad")


class PrecioMercado(Base):
    __tablename__ = "precios_mercado"
    __table_args__ = (UniqueConstraint("producto", "fecha", name="uq_precio_producto_fecha"),)

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )
    producto: Mapped[str] = mapped_column(String(30), nullable=False)  # 'choclo' | 'maiz_duro'
    fuente: Mapped[str] = mapped_column(String(120), nullable=False)
    presentacion: Mapped[str] = mapped_column(String(60), nullable=True)
    precio: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    precio_min: Mapped[float] = mapped_column(Numeric(8, 2), nullable=True)
    precio_max: Mapped[float] = mapped_column(Numeric(8, 2), nullable=True)
    variedades: Mapped[str] = mapped_column(Text, nullable=True)
    fecha: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=False), server_default=func.now())



class Consulta(Base):
    """Consulta de un usuario (normalmente un agricultor) a un experto sobre uno de sus análisis.

    `resumen` guarda una foto del análisis al enviarlo: los análisis por parcela viven en el navegador
    y el análisis individual puede borrarse después (entonces `analisis_id` pasa a NULL).
    Todas las marcas de tiempo se escriben desde Python en UTC naive para poder compararlas entre sí.
    """
    __tablename__ = "consultas"
    __table_args__ = (
        CheckConstraint("tipo IN ('individual', 'parcela')", name="ck_consulta_tipo"),
        CheckConstraint("estado IN ('abierta', 'cerrada')", name="ck_consulta_estado"),
        CheckConstraint("solicitante_id <> experto_id", name="ck_consulta_participantes"),
        CheckConstraint(
            "dictamen_diagnostico IS NULL OR dictamen_diagnostico IN ('Fondo', 'Tizon', 'Roya', 'Mancha_blanca', 'Otro')",
            name="ck_consulta_dictamen_diagnostico",
        ),
        Index("ix_consultas_solicitante_actividad", "solicitante_id", "ultimo_mensaje_at"),
        Index("ix_consultas_experto_actividad", "experto_id", "ultimo_mensaje_at"),
        # Una sola consulta abierta por análisis (o visita de parcela) y experto: evita duplicados por doble clic
        Index(
            "uq_consulta_abierta_analisis", "solicitante_id", "experto_id", "analisis_id", unique=True,
            postgresql_where=text("estado = 'abierta' AND analisis_id IS NOT NULL"),
        ),
        Index(
            "uq_consulta_abierta_referencia", "solicitante_id", "experto_id", "referencia", unique=True,
            postgresql_where=text("estado = 'abierta' AND referencia IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )
    solicitante_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False)
    experto_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False)
    tipo: Mapped[str] = mapped_column(String(20), nullable=False)  # 'individual' | 'parcela'
    # Si el dueño borra el análisis, la consulta se conserva con su resumen
    analisis_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("analisis.id", ondelete="SET NULL"), nullable=True)
    # Identificador de la visita de parcela en el navegador (para no enviar dos veces la misma)
    referencia: Mapped[str] = mapped_column(String(100), nullable=True)
    resumen: Mapped[dict] = mapped_column(JSONB, nullable=False)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'abierta'"))
    # Dictamen del experto: su propio diagnóstico, independiente del modelo
    dictamen_coincide: Mapped[bool] = mapped_column(Boolean, nullable=True)
    dictamen_diagnostico: Mapped[str] = mapped_column(String(30), nullable=True)
    dictamen_texto: Mapped[str] = mapped_column(Text, nullable=True)
    dictamen_fecha: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=True)
    # Última vez que cada participante leyó la conversación (NULL en el experto = consulta nueva sin abrir)
    leido_solicitante_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=True)
    leido_experto_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=True)
    ultimo_mensaje_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False)
    cerrada_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=True)


class MensajeConsulta(Base):
    """Mensaje del chat de una consulta: texto, imagen (data URL) o ambos."""
    __tablename__ = "mensajes_consulta"
    __table_args__ = (
        CheckConstraint("texto IS NOT NULL OR imagen_url IS NOT NULL", name="ck_mensaje_contenido"),
        Index("ix_mensajes_consulta_fecha", "consulta_id", "created_at"),
        # Un reintento del mismo envío (mismo client_id) no crea un mensaje duplicado
        Index(
            "uq_mensaje_client_id", "consulta_id", "autor_id", "client_id", unique=True,
            postgresql_where=text("client_id IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("uuid_generate_v4()"),
    )
    consulta_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("consultas.id", ondelete="CASCADE"), nullable=False)
    autor_id: Mapped[str] = mapped_column(UUID(as_uuid=True), ForeignKey("usuarios.id", ondelete="CASCADE"), nullable=False)
    texto: Mapped[str] = mapped_column(Text, nullable=True)
    imagen_url: Mapped[str] = mapped_column(Text, nullable=True)
    client_id: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False)

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


async def seed_enfermedades(session: AsyncSession) -> None:
    default_enfermedades = [
        {
            "nombre": "Fondo",
            "clase_idx": 0,
            "descripcion": "Área sin cultivo o píxeles de fondo sin enfermedad",
        },
        {
            "nombre": "Tizon",
            "clase_idx": 1,
            "descripcion": "Tizón del maíz — Exserohilum turcicum",
        },
        {
            "nombre": "Roya",
            "clase_idx": 2,
            "descripcion": "Roya común del maíz — Puccinia sorghi",
        },
        {
            "nombre": "Mancha_blanca",
            "clase_idx": 3,
            "descripcion": "Mancha blanca del maíz — Phaeosphaeria maydis",
        },
    ]

    for enfermedad_data in default_enfermedades:
        result = await session.execute(
            text(
                "SELECT id FROM enfermedades WHERE clase_idx = :clase_idx OR nombre = :nombre"
            ),
            enfermedad_data,
        )
        if result.first() is None:
            await session.execute(
                text(
                    "INSERT INTO enfermedades (nombre, clase_idx, descripcion) VALUES (:nombre, :clase_idx, :descripcion)"
                ),
                enfermedad_data,
            )
    await session.commit()
