import os
from typing import AsyncGenerator

from dotenv import load_dotenv

load_dotenv()

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, SmallInteger,
                        String, Text, func, text, Numeric)
from sqlalchemy.dialects.postgresql import UUID, ENUM
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DATABASE_NAME = os.getenv("DATABASE_NAME", "bd_tesis")
DATABASE_USER = os.getenv("DATABASE_USER", "postgres")
DATABASE_PASSWORD = os.getenv("DATABASE_PASSWORD", "postgres")
DATABASE_HOST = os.getenv("DATABASE_HOST", "localhost")
DATABASE_PORT = os.getenv("DATABASE_PORT", "5432")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql+asyncpg://{DATABASE_USER}:{DATABASE_PASSWORD}@{DATABASE_HOST}:{DATABASE_PORT}/{DATABASE_NAME}",
)

# Render (and many cloud providers) supply postgresql:// or postgres://
# but SQLAlchemy async needs the postgresql+asyncpg:// dialect prefix.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

engine = create_async_engine(DATABASE_URL, future=True, echo=False)
async_session = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)

rol_usuario_enum = ENUM(
    "investigador",
    "director",
    "admin",
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
        server_default=text("'investigador'::rol_usuario"),
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
    afectacion_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False)
    severidad: Mapped[str] = mapped_column(nivel_severidad_enum, nullable=False)
    fecha: Mapped[str] = mapped_column(DateTime(timezone=False), nullable=False, server_default=func.now())

    user: Mapped[Usuario] = relationship("Usuario", back_populates="analisis")
    enfermedad: Mapped[Enfermedad] = relationship("Enfermedad")


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
