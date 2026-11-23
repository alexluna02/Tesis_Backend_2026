import asyncio
import hashlib
import hmac
import math
import os
import secrets
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import auth
import consultas
import database
import mercado
import schemas
from model import CFG, GRADCAM_ERROR, XAI_AVAILABLE, generate_xai, load_model, predict_from_bytes

# ── SMTP config ───────────────────────────────────────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

RESET_CODE_EXPIRE_MINUTES = 15
RESET_CODE_MAX_ATTEMPTS = 5
RESET_CODE_RESEND_SECONDS = 60
FORGOT_PASSWORD_MSG = "Si el correo está registrado, recibirás un código de verificación."
RESET_CODE_EXPIRED_MSG = "El código expiró o superó el número de intentos. Solicita uno nuevo."


def _build_reset_email_html(code: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f8fafc;padding:20px;margin:0">
  <div style="max-width:480px;margin:0 auto;background:white;border-radius:20px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08)">
    <div style="background:linear-gradient(135deg,#7c3aed,#22c55e);padding:28px 24px;text-align:center">
      <h1 style="color:white;margin:0;font-size:22px">🌿 AgroScan</h1>
      <p style="color:rgba(255,255,255,0.85);margin:6px 0 0;font-size:13px">Recuperación de contraseña</p>
    </div>
    <div style="padding:32px 28px">
      <p style="color:#0f172a;font-size:15px;margin:0 0 8px">Hola,</p>
      <p style="color:#475569;font-size:14px;margin:0 0 24px">Recibimos una solicitud para restablecer la contraseña de tu cuenta <strong>AgroScan</strong>. Usa el siguiente código:</p>
      <div style="background:#f3f0ff;border:1.5px solid #c4b5fd;border-radius:14px;padding:20px;text-align:center;margin:0 0 24px">
        <p style="color:#6b7280;font-size:11px;letter-spacing:2px;text-transform:uppercase;margin:0 0 8px">Código de verificación</p>
        <span style="font-size:40px;font-weight:900;letter-spacing:12px;color:#7c3aed">{code}</span>
      </div>
      <p style="color:#64748b;font-size:13px;margin:0 0 4px">⏱ Este código expira en <strong>{RESET_CODE_EXPIRE_MINUTES} minutos</strong>.</p>
      <p style="color:#94a3b8;font-size:12px;margin:0">Si no solicitaste esto, puedes ignorar este correo.</p>
    </div>
    <div style="background:#f8fafc;padding:16px 24px;text-align:center;border-top:1px solid #e2e8f0">
      <p style="color:#94a3b8;font-size:11px;margin:0">AgroScan · Detección de Enfermedades en Maíz · UAV · IA</p>
    </div>
  </div>
</body></html>"""


def _send_email_sync(to: str, subject: str, html: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SMTP_USER
    msg["To"]      = to
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as srv:
        srv.ehlo()
        srv.starttls()
        srv.login(SMTP_USER, SMTP_PASSWORD)
        srv.sendmail(SMTP_USER, to, msg.as_string())


async def send_reset_email(to: str, code: str) -> None:
    if not SMTP_USER or not SMTP_PASSWORD or SMTP_USER.startswith("TU_CORREO"):
        print(f"[AgroScan] SMTP no configurado. Código de recuperación para {to}: {code}")
        return
    html = _build_reset_email_html(code)
    await asyncio.to_thread(_send_email_sync, to, "Código de recuperación - AgroScan", html)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash_reset_code(user_id, code: str) -> str:
    # HMAC con la clave del servidor: aunque se filtrara la tabla, los 10^6 códigos
    # posibles no se pueden probar sin conocer JWT_SECRET_KEY.
    return hmac.new(auth.SECRET_KEY.encode(), f"{user_id}:{code}".encode(), hashlib.sha256).hexdigest()


async def _send_reset_email_safe(to: str, code: str) -> None:
    try:
        await send_reset_email(to, code)
    except Exception as exc:
        print(f"[AgroScan] Error enviando el código de recuperación a {to}: {exc}")


async def _check_reset_code(db: AsyncSession, email: str, code: str) -> tuple[database.Usuario, UUID]:
    """Valida el código vigente del usuario gastando un intento.

    El intento se descuenta con un UPDATE condicional (atómico en PostgreSQL), así que
    ni enviando cientos de peticiones en paralelo se superan RESET_CODE_MAX_ATTEMPTS.
    """
    result = await db.execute(select(database.Usuario).where(database.Usuario.email == email))
    user = result.scalars().first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=RESET_CODE_EXPIRED_MSG)

    reset_id = (await db.execute(
        select(database.PasswordReset.id)
        .where(
            database.PasswordReset.user_id == user.id,
            database.PasswordReset.usado.is_(False),
            database.PasswordReset.expires_at > _utcnow(),
        )
        .order_by(database.PasswordReset.created_at.desc())
        .limit(1)
    )).scalar()
    if reset_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=RESET_CODE_EXPIRED_MSG)

    reserved = (await db.execute(
        update(database.PasswordReset)
        .where(
            database.PasswordReset.id == reset_id,
            database.PasswordReset.usado.is_(False),
            database.PasswordReset.intentos < RESET_CODE_MAX_ATTEMPTS,
        )
        .values(intentos=database.PasswordReset.intentos + 1)
        .returning(database.PasswordReset.code_hash, database.PasswordReset.intentos)
        .execution_options(synchronize_session=False)
    )).first()
    await db.commit()

    if reserved is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=RESET_CODE_EXPIRED_MSG)
    if not hmac.compare_digest(reserved.code_hash, _hash_reset_code(user.id, code)):
        restantes = RESET_CODE_MAX_ATTEMPTS - reserved.intentos
        if restantes <= 0:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=RESET_CODE_EXPIRED_MSG)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Código incorrecto. Te queda{'n' if restantes > 1 else ''} {restantes} intento{'s' if restantes > 1 else ''}.",
        )
    return user, reset_id


app = FastAPI(
    title="ProyectoTesis API",
    description="API para inferencia de modelo de segmentación y registro de análisis de maíz",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Consultas a expertos: /expertos y /consultas/* (ver consultas.py)
app.include_router(consultas.router)


def _sin_no_finitos(valor):
    """NaN e infinito no existen en JSON: se devuelven como texto."""
    if isinstance(valor, float) and not math.isfinite(valor):
        return str(valor)
    if isinstance(valor, dict):
        return {k: _sin_no_finitos(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_sin_no_finitos(v) for v in valor]
    return valor


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    # Igual que el manejador de FastAPI (422 con {"detail": errores}), pero si el cliente envió NaN o
    # infinito el error ya no rompe la respuesta con un 500 al no poder convertirlo a JSON.
    return JSONResponse(status_code=422, content={"detail": _sin_no_finitos(jsonable_encoder(exc.errors()))})


MODEL_PATH = os.environ.get("MODEL_PATH", "Mit_B0_384_Test.pth")
model = None
MODEL_LOAD_TIME_MS: float | None = None


async def _ensure_schema() -> None:
    """Crea o actualiza el esquema de forma idempotente (se ejecuta en cada arranque).

    Las tablas nuevas (p. ej. consultas y mensajes_consulta) las crea create_all; las columnas y
    valores de ENUM añadidos después se aplican con sentencias IF NOT EXISTS.
    """
    async with database.engine.begin() as conn:
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
        await conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE rol_usuario AS ENUM ('agricultor', 'investigador', 'director', 'admin', 'experto');
            EXCEPTION WHEN duplicate_object THEN null; END $$
        """))
        await conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE nivel_severidad AS ENUM ('leve', 'moderada', 'grave');
            EXCEPTION WHEN duplicate_object THEN null; END $$
        """))
        await conn.run_sync(database.Base.metadata.create_all)
    # 'agricultor' was added after the enum was first created; the new value
    # must be committed before it can be used as the column default.
    async with database.engine.begin() as conn:
        await conn.execute(text("ALTER TYPE rol_usuario ADD VALUE IF NOT EXISTS 'agricultor'"))
    # Rol de los expertos que atienden las consultas (en su propia transacción, como el anterior)
    async with database.engine.begin() as conn:
        await conn.execute(text("ALTER TYPE rol_usuario ADD VALUE IF NOT EXISTS 'experto'"))
    async with database.engine.begin() as conn:
        await conn.execute(text("ALTER TABLE usuarios ALTER COLUMN rol SET DEFAULT 'agricultor'"))
    # Columna añadida después de crear la tabla: create_all no altera tablas existentes
    async with database.engine.begin() as conn:
        await conn.execute(text("ALTER TABLE analisis ADD COLUMN IF NOT EXISTS resultado_url TEXT"))
    # Chat general con expertos y documentos PDF: columnas nuevas en tablas que ya existen, y las dos
    # restricciones que cambiaron (se reemplazan solo si aún tienen la definición anterior)
    async with database.engine.begin() as conn:
        for sentencia in (
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS especialidad VARCHAR(80)",
            "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS ultima_actividad TIMESTAMP WITHOUT TIME ZONE",
            "ALTER TABLE mensajes_consulta ADD COLUMN IF NOT EXISTS documento_nombre VARCHAR(160)",
            "ALTER TABLE mensajes_consulta ADD COLUMN IF NOT EXISTS documento_tamano INTEGER",
            "ALTER TABLE mensajes_consulta ADD COLUMN IF NOT EXISTS documento_datos BYTEA",
        ):
            await conn.execute(text(sentencia))
        await conn.execute(text("""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_consulta_tipo'
                               AND pg_get_constraintdef(oid) LIKE '%general%') THEN
                    ALTER TABLE consultas DROP CONSTRAINT IF EXISTS ck_consulta_tipo;
                    ALTER TABLE consultas ADD CONSTRAINT ck_consulta_tipo
                        CHECK (tipo IN ('individual', 'parcela', 'general'));
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mensaje_contenido'
                               AND pg_get_constraintdef(oid) LIKE '%documento_datos%') THEN
                    ALTER TABLE mensajes_consulta DROP CONSTRAINT IF EXISTS ck_mensaje_contenido;
                    ALTER TABLE mensajes_consulta ADD CONSTRAINT ck_mensaje_contenido
                        CHECK (texto IS NOT NULL OR imagen_url IS NOT NULL OR documento_datos IS NOT NULL);
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_mensaje_documento') THEN
                    ALTER TABLE mensajes_consulta ADD CONSTRAINT ck_mensaje_documento
                        CHECK ((documento_datos IS NULL) = (documento_nombre IS NULL));
                END IF;
            END $$
        """))
        await conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_consulta_general_abierta ON consultas (solicitante_id, experto_id)
            WHERE estado = 'abierta' AND tipo = 'general'
        """))


@app.on_event("startup")
async def startup_event():
    global model

    if auth.SECRET_KEY == auth.DEFAULT_SECRET_KEY:
        print("[startup] ADVERTENCIA: JWT_SECRET_KEY no está configurada. Se usa una clave por defecto "
              "pública y cualquiera podría firmar tokens válidos. Configúrala en los secrets del despliegue.")

    # ── Auto-create schema (idempotent — safe to run every startup) ──────────
    # Required the first time the app connects to a fresh cloud DB (e.g. Render).
    try:
        await _ensure_schema()
        print("[startup] Schema listo")
    except Exception as exc:
        print(f"[startup] Error creando schema: {exc}")

    # ── Load ML model ─────────────────────────────────────────────────────────
    global MODEL_LOAD_TIME_MS
    try:
        _t0 = time.perf_counter()
        model = load_model(MODEL_PATH, device=CFG.device)
        MODEL_LOAD_TIME_MS = round((time.perf_counter() - _t0) * 1000, 1)
        print(f"[startup] Modelo cargado en {MODEL_LOAD_TIME_MS} ms")
    except FileNotFoundError:
        model = None
        print(f"[startup] Modelo no encontrado en {MODEL_PATH}")

    # ── Seed disease catalog ──────────────────────────────────────────────────
    try:
        async with database.async_session() as session:
            await database.seed_enfermedades(session)
    except Exception as exc:
        print(f"[startup] Error inicializando la base de datos: {exc}")

    # ── Precios de mercado: banda oficial de maíz duro y, en segundo plano para no retrasar
    #    el arranque, los boletines diarios de los últimos 120 días (10 productos) ──────────
    try:
        async with database.async_session() as session:
            await mercado.ensure_maiz_duro_seed(session)
    except Exception as exc:
        print(f"[startup] Error preparando precios de mercado: {exc}")
    mercado.iniciar_sincronizacion_inicial(dias=120)


@app.get("/", tags=["general"])
def read_root():
    return {"mensaje": "API lista. Visita /docs para la documentación interactiva."}


@app.get("/health", tags=["general"])
async def health_check(db: AsyncSession = Depends(database.get_db)):
    db_ready = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception:
        db_ready = False

    return {
        "status": "ok",
        "device": str(CFG.device),
        "model_loaded": model is not None,
        "model_path": MODEL_PATH,
        "model_load_time_ms": MODEL_LOAD_TIME_MS,
        "database_ready": db_ready,
    }


@app.post("/auth/register", response_model=schemas.UserRead, tags=["auth"], status_code=status.HTTP_201_CREATED)
async def register_user(user_create: schemas.UserCreate, db: AsyncSession = Depends(database.get_db)):
    result = await db.execute(select(database.Usuario).where(database.Usuario.email == user_create.email))
    existing_user = result.scalars().first()
    if existing_user:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El correo ya está registrado")

    user = database.Usuario(
        nombre=user_create.nombre,
        email=user_create.email,
        password_hash=auth.get_password_hash(user_create.password),
        rol="agricultor",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@app.post("/auth/login", response_model=schemas.Token, tags=["auth"])
async def login(login_data: schemas.LoginRequest, db: AsyncSession = Depends(database.get_db)):
    user = await auth.authenticate_user(db, login_data.email, login_data.password)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Correo o contraseña inválidos")
    if not user.activo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Usuario desactivado")

    access_token = auth.create_access_token(
        data={"sub": str(user.id), "email": user.email},
        expires_delta=timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = auth.create_refresh_token()
    refresh_row = database.RefreshToken(
        user_id=user.id,
        token=refresh_token,
        expires_at=datetime.utcnow() + timedelta(days=auth.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(refresh_row)
    await db.commit()
    return schemas.Token(access_token=access_token, refresh_token=refresh_token)


@app.post("/auth/refresh", response_model=schemas.Token, tags=["auth"])
async def refresh_token(request: schemas.RefreshRequest, db: AsyncSession = Depends(database.get_db)):
    user, new_refresh_token = await auth.rotate_refresh_token(db, request.refresh_token)
    access_token = auth.create_access_token(data={"sub": str(user.id), "email": user.email})
    return schemas.Token(access_token=access_token, refresh_token=new_refresh_token)


@app.post("/auth/logout", tags=["auth"])
async def logout(request: schemas.LogoutRequest, db: AsyncSession = Depends(database.get_db)):
    await auth.revoke_refresh_token(db, request.refresh_token)
    return {"detail": "Sesión cerrada"}


@app.post("/auth/google", response_model=schemas.Token, tags=["auth"])
async def login_with_google(request: schemas.GoogleLoginRequest, db: AsyncSession = Depends(database.get_db)):
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {request.access_token}"},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token de Google inválido")

    google_data = resp.json()
    email = google_data.get("email")
    nombre = google_data.get("name") or email

    if not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No se pudo obtener el email de Google")

    result = await db.execute(select(database.Usuario).where(database.Usuario.email == email))
    user = result.scalars().first()

    if user is None:
        user = database.Usuario(
            nombre=nombre,
            email=email,
            password_hash=auth.get_password_hash(secrets.token_urlsafe(32)),
            rol="agricultor",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    if not user.activo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Usuario desactivado")

    access_token = auth.create_access_token(
        data={"sub": str(user.id), "email": user.email},
        expires_delta=timedelta(minutes=auth.ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    refresh_token = auth.create_refresh_token()
    refresh_row = database.RefreshToken(
        user_id=user.id,
        token=refresh_token,
        expires_at=datetime.utcnow() + timedelta(days=auth.REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(refresh_row)
    await db.commit()
    return schemas.Token(access_token=access_token, refresh_token=refresh_token)


@app.get("/users/me", response_model=schemas.UserRead, tags=["auth"])
async def get_me(current_user: database.Usuario = Depends(auth.get_current_active_user)):
    return current_user


@app.put("/users/me", response_model=schemas.UserRead, tags=["auth"])
async def update_profile(
    update: schemas.UserUpdate,
    current_user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    current_user.nombre = update.nombre.strip()
    # La especialidad solo cambia si viene en la petición (vacía = quitarla)
    if "especialidad" in update.model_fields_set:
        current_user.especialidad = (update.especialidad or "").strip() or None
    current_user.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(current_user)
    return current_user


@app.put("/users/me/password", tags=["auth"])
async def change_password(
    req: schemas.PasswordChange,
    current_user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    if not auth.verify_password(req.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Contraseña actual incorrecta")
    current_user.password_hash = auth.get_password_hash(req.new_password)
    current_user.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    return {"detail": "Contraseña actualizada correctamente"}


# ── Admin: gestión de usuarios ────────────────────────────────────────────────
# La cuenta admin es única: desde aquí solo se asignan los roles agricultor,
# investigador y experto, y el admin no puede modificarse a sí mismo ni a otro admin.

async def _get_managed_user(db: AsyncSession, user_id: UUID, admin: database.Usuario) -> database.Usuario:
    user = await db.get(database.Usuario, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado")
    if user.id == admin.id or user.rol == "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No se puede modificar una cuenta de administrador")
    return user


@app.get("/admin/users", response_model=List[schemas.UserRead], tags=["admin"])
async def admin_list_users(
    _: database.Usuario = Depends(auth.get_current_active_admin),
    db: AsyncSession = Depends(database.get_db),
):
    result = await db.execute(select(database.Usuario).order_by(database.Usuario.created_at.desc()))
    return result.scalars().all()


@app.post("/admin/users", response_model=schemas.UserRead, tags=["admin"], status_code=status.HTTP_201_CREATED)
async def admin_create_user(
    req: schemas.AdminUserCreate,
    _: database.Usuario = Depends(auth.get_current_active_admin),
    db: AsyncSession = Depends(database.get_db),
):
    result = await db.execute(select(database.Usuario).where(database.Usuario.email == req.email))
    if result.scalars().first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El correo ya está registrado")

    user = database.Usuario(
        nombre=req.nombre.strip(),
        email=req.email,
        password_hash=auth.get_password_hash(req.password),
        rol=req.rol,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@app.patch("/admin/users/{user_id}", response_model=schemas.UserRead, tags=["admin"])
async def admin_update_user(
    user_id: UUID,
    req: schemas.AdminUserUpdate,
    admin: database.Usuario = Depends(auth.get_current_active_admin),
    db: AsyncSession = Depends(database.get_db),
):
    user = await _get_managed_user(db, user_id, admin)
    if req.rol is not None:
        user.rol = req.rol
    if req.activo is not None:
        user.activo = req.activo
    user.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db.commit()
    await db.refresh(user)
    return user


@app.post("/auth/forgot-password", tags=["auth"])
async def forgot_password(
    request: schemas.ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(database.get_db),
):
    # Siempre la misma respuesta, y el correo se envía en segundo plano para que ni el
    # contenido ni el tiempo de respuesta revelen si la cuenta existe.
    result = await db.execute(select(database.Usuario).where(database.Usuario.email == request.email))
    user = result.scalars().first()
    if user is None or not user.activo:
        return {"detail": FORGOT_PASSWORD_MSG}

    now = _utcnow()
    ultimo_envio = (await db.execute(
        select(database.PasswordReset.created_at)
        .where(database.PasswordReset.user_id == user.id)
        .order_by(database.PasswordReset.created_at.desc())
        .limit(1)
    )).scalar()
    if ultimo_envio is not None and (now - ultimo_envio).total_seconds() < RESET_CODE_RESEND_SECONDS:
        return {"detail": FORGOT_PASSWORD_MSG}

    # Un solo código vigente por usuario: pedir uno nuevo anula los anteriores
    await db.execute(
        update(database.PasswordReset)
        .where(database.PasswordReset.user_id == user.id, database.PasswordReset.usado.is_(False))
        .values(usado=True)
        .execution_options(synchronize_session=False)
    )
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(database.PasswordReset(
        user_id=user.id,
        code_hash=_hash_reset_code(user.id, code),
        expires_at=now + timedelta(minutes=RESET_CODE_EXPIRE_MINUTES),
        created_at=now,
    ))
    await db.commit()

    background_tasks.add_task(_send_reset_email_safe, user.email, code)
    return {"detail": FORGOT_PASSWORD_MSG}


@app.post("/auth/verify-reset-code", tags=["auth"])
async def verify_reset_code(
    request: schemas.ResetCodeRequest,
    db: AsyncSession = Depends(database.get_db),
):
    """Comprueba el código sin cambiar la contraseña (paso 2 del asistente de recuperación)."""
    _, reset_id = await _check_reset_code(db, request.email, request.code)
    # Un código correcto no gasta intento: debe seguir sirviendo en el paso final
    await db.execute(
        update(database.PasswordReset)
        .where(database.PasswordReset.id == reset_id)
        .values(intentos=database.PasswordReset.intentos - 1)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return {"detail": "Código verificado."}


@app.post("/auth/reset-password", tags=["auth"])
async def reset_password(
    request: schemas.ResetPasswordRequest,
    db: AsyncSession = Depends(database.get_db),
):
    user, reset_id = await _check_reset_code(db, request.email, request.code)

    await db.execute(
        update(database.PasswordReset)
        .where(database.PasswordReset.id == reset_id)
        .values(usado=True)
        .execution_options(synchronize_session=False)
    )
    user.password_hash = auth.get_password_hash(request.new_password)
    user.updated_at = _utcnow()
    # Cierra todas las sesiones abiertas: si alguien más tenía acceso a la cuenta, lo pierde
    await auth.revoke_all_refresh_tokens(db, user.id)
    await db.commit()
    return {"detail": "Contraseña actualizada correctamente. Ya puedes iniciar sesión."}


@app.get("/enfermedades", response_model=List[schemas.EnfermedadRead], tags=["catalogo"])
async def list_enfermedades(db: AsyncSession = Depends(database.get_db)):
    result = await db.execute(select(database.Enfermedad).order_by(database.Enfermedad.clase_idx))
    return result.scalars().all()


@app.post("/analisis", response_model=schemas.AnalisisRead, tags=["analisis"])
async def create_analysis(
    payload: schemas.AnalisisCreate,
    current_user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    result = await db.execute(select(database.Enfermedad).where(database.Enfermedad.id == payload.enfermedad_id))
    enfermedad = result.scalars().first()
    if enfermedad is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Enfermedad no encontrada")

    analisis = database.Analisis(
        user_id=current_user.id,
        enfermedad_id=enfermedad.id,
        imagen_url=payload.imagen_url,
        grad_cam_url=payload.grad_cam_url,
        resultado_url=payload.resultado_url,
        afectacion_pct=payload.afectacion_pct,
        severidad=payload.severidad,
    )
    db.add(analisis)
    await db.commit()

    loaded = await db.execute(
        select(database.Analisis)
        .options(selectinload(database.Analisis.enfermedad))
        .where(database.Analisis.id == analisis.id)
    )
    return loaded.scalars().first()


@app.get("/analisis", response_model=List[schemas.AnalisisRead], tags=["analisis"])
async def get_analisis(current_user: database.Usuario = Depends(auth.get_current_active_user), db: AsyncSession = Depends(database.get_db)):
    result = await db.execute(
        select(database.Analisis)
        .options(selectinload(database.Analisis.enfermedad))
        .where(database.Analisis.user_id == current_user.id)
        .order_by(database.Analisis.fecha.desc())
    )
    return result.scalars().all()


@app.delete("/analisis/{analisis_id}", tags=["analisis"], status_code=status.HTTP_204_NO_CONTENT)
async def delete_analysis(
    analisis_id: str,
    current_user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    result = await db.execute(
        select(database.Analisis).where(database.Analisis.id == analisis_id, database.Analisis.user_id == current_user.id)
    )
    analisis = result.scalars().first()
    if analisis is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Análisis no encontrado")
    await db.delete(analisis)
    await db.commit()
    return {"detail": "Análisis eliminado"}


@app.get("/historial", response_model=List[schemas.AnalisisRead], tags=["analisis"])
async def get_historial(current_user: database.Usuario = Depends(auth.get_current_active_user), db: AsyncSession = Depends(database.get_db)):
    return await get_analisis(current_user=current_user, db=db)


@app.get("/stats", response_model=schemas.StatsResponse, tags=["analisis"])
async def get_stats(current_user: database.Usuario = Depends(auth.get_current_active_user), db: AsyncSession = Depends(database.get_db)):
    result = await db.execute(
        select(database.Analisis)
        .options(selectinload(database.Analisis.enfermedad))
        .where(database.Analisis.user_id == current_user.id)
    )
    records = result.scalars().all()

    total = len(records)
    detecciones = sum(1 for item in records if item.enfermedad and item.enfermedad.clase_idx > 0)
    sanos = sum(1 for item in records if item.enfermedad and item.enfermedad.clase_idx == 0)
    promedio = None
    if total > 0:
        promedio = round(sum(float(item.afectacion_pct) for item in records) / total, 2)

    return schemas.StatsResponse(
        total_analisis=total,
        detecciones_enfermedad=detecciones,
        cultivos_sanos=sanos,
        afectacion_promedio=promedio,
    )


async def _sincronizar_mercado(db: AsyncSession, dias: int = 3) -> None:
    """Trae los boletines recientes que falten; si falla, la página usa lo que ya está guardado."""
    try:
        await mercado.sync_mercado(db, days_back=dias)
    except Exception as exc:
        await db.rollback()
        print(f"[mercado] Error sincronizando precios: {exc}")


@app.get("/mercado/resumen", response_model=schemas.MercadoResumen, tags=["mercado"])
async def get_mercado_resumen(db: AsyncSession = Depends(database.get_db)):
    await _sincronizar_mercado(db)
    return await mercado.get_resumen(db)


@app.get("/mercado/productos", response_model=schemas.MercadoProductos, tags=["mercado"])
async def get_mercado_productos(dias: int = 90, db: AsyncSession = Depends(database.get_db)):
    """Los 10 productos de AgroScan con su último precio, la variación semanal y la serie de «dias»."""
    await _sincronizar_mercado(db)
    return await mercado.get_productos(db, dias)


@app.get("/mercado/historico", response_model=List[schemas.PrecioPunto], tags=["mercado"])
async def get_mercado_historico(
    producto: str = "choclo",
    dias: int = 30,
    db: AsyncSession = Depends(database.get_db),
):
    if producto != "maiz_duro" and producto not in mercado.CLAVES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Producto inválido. Use 'maiz_duro' o uno de: {', '.join(mercado.CLAVES)}.",
        )
    dias = max(1, min(dias, 365))
    if producto != "maiz_duro":
        await _sincronizar_mercado(db)
    return await mercado.get_historico(db, producto, dias)


@app.post("/predict/segmentation", tags=["inference"])
async def predict_segmentation(
    file: UploadFile = File(...),
    current_user: database.Usuario = Depends(auth.get_current_active_user),
):
    if model is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Modelo no está cargado")

    t_request = time.perf_counter()
    try:
        content = await file.read()
        t_inference = time.perf_counter()
        result = predict_from_bytes(model, content)
        inference_ms = round((time.perf_counter() - t_inference) * 1000, 1)
        result["timing"] = {
            "inference_time_ms": inference_ms,
            "api_response_time_ms": round((time.perf_counter() - t_request) * 1000, 1),
        }
        return result
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@app.post("/predict/xai", tags=["inference"])
async def predict_xai(
    file: UploadFile = File(...),
    target_class: int | None = None,
    current_user: database.Usuario = Depends(auth.get_current_active_user),
):
    if model is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Modelo no está cargado")
    if not XAI_AVAILABLE:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=GRADCAM_ERROR or "XAI no está disponible")

    t_request = time.perf_counter()
    try:
        content = await file.read()
        t_inference = time.perf_counter()
        result = generate_xai(model, content, target_class=target_class)
        if "error" in result:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=result["error"])
        inference_ms = round((time.perf_counter() - t_inference) * 1000, 1)
        result["timing"] = {
            "inference_time_ms": inference_ms,
            "api_response_time_ms": round((time.perf_counter() - t_request) * 1000, 1),
        }
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
