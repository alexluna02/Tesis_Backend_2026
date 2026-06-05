import asyncio
import os
import random
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

import httpx
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import auth
import database
import schemas
from model import CFG, GRADCAM_ERROR, XAI_AVAILABLE, generate_xai, load_model, predict_from_bytes

# ── SMTP config ───────────────────────────────────────────────────────────────
SMTP_HOST     = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT     = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER     = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")

RESET_CODE_EXPIRE_MINUTES = 15


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


def create_reset_token(email: str, code: str) -> str:
    payload = {
        "sub":     email,
        "code":    code,
        "purpose": "reset",
        "exp":     datetime.utcnow() + timedelta(minutes=RESET_CODE_EXPIRE_MINUTES),
    }
    return auth.jwt.encode(payload, auth.SECRET_KEY, algorithm=auth.ALGORITHM)


def verify_reset_token(token: str, code: str) -> str:
    try:
        from jose import JWTError, jwt as _jwt
        payload = _jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
        if payload.get("purpose") != "reset":
            raise HTTPException(status_code=400, detail="Token inválido")
        if payload.get("code") != code:
            raise HTTPException(status_code=400, detail="Código incorrecto")
        return payload["sub"]
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=400, detail="Token inválido o expirado")


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

MODEL_PATH = os.environ.get("MODEL_PATH", "best_model.pth")
model = None


@app.on_event("startup")
async def startup_event():
    global model

    # ── Auto-create schema (idempotent — safe to run every startup) ──────────
    # Required the first time the app connects to a fresh cloud DB (e.g. Render).
    try:
        async with database.engine.begin() as conn:
            await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
            await conn.execute(text("""
                DO $$ BEGIN
                    CREATE TYPE rol_usuario AS ENUM ('investigador', 'director', 'admin');
                EXCEPTION WHEN duplicate_object THEN null; END $$
            """))
            await conn.execute(text("""
                DO $$ BEGIN
                    CREATE TYPE nivel_severidad AS ENUM ('leve', 'moderada', 'grave');
                EXCEPTION WHEN duplicate_object THEN null; END $$
            """))
            await conn.run_sync(database.Base.metadata.create_all)
        print("[startup] Schema listo")
    except Exception as exc:
        print(f"[startup] Error creando schema: {exc}")

    # ── Load ML model ─────────────────────────────────────────────────────────
    try:
        model = load_model(MODEL_PATH, device=CFG.device)
    except FileNotFoundError:
        model = None
        print(f"[startup] Modelo no encontrado en {MODEL_PATH}")

    # ── Seed disease catalog ──────────────────────────────────────────────────
    try:
        async with database.async_session() as session:
            await database.seed_enfermedades(session)
    except Exception as exc:
        print(f"[startup] Error inicializando la base de datos: {exc}")


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


@app.post("/auth/forgot-password", tags=["auth"])
async def forgot_password(
    request: schemas.ForgotPasswordRequest,
    db: AsyncSession = Depends(database.get_db),
):
    result = await db.execute(select(database.Usuario).where(database.Usuario.email == request.email))
    user = result.scalars().first()

    # Respuesta genérica aunque el correo no exista (evita enumeración de usuarios)
    if user is None:
        return {"detail": "Si el correo existe, recibirás un código de verificación."}

    code        = str(random.randint(100000, 999999))
    reset_token = create_reset_token(user.email, code)

    try:
        await send_reset_email(user.email, code)
    except Exception as exc:
        print(f"[AgroScan] Error enviando email: {exc}. Código: {code}")

    return {"reset_token": reset_token, "detail": "Código enviado. Revisa tu bandeja de entrada."}


@app.post("/auth/reset-password", tags=["auth"])
async def reset_password(
    request: schemas.ResetPasswordRequest,
    db: AsyncSession = Depends(database.get_db),
):
    email = verify_reset_token(request.reset_token, request.code)

    result = await db.execute(select(database.Usuario).where(database.Usuario.email == email))
    user = result.scalars().first()
    if user is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    user.password_hash = auth.get_password_hash(request.new_password)
    user.updated_at    = datetime.now(timezone.utc).replace(tzinfo=None)
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


@app.post("/predict/segmentation", tags=["inference"])
async def predict_segmentation(
    file: UploadFile = File(...),
    current_user: database.Usuario = Depends(auth.get_current_active_user),
):
    if model is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Modelo no está cargado")

    try:
        content = await file.read()
        result = predict_from_bytes(model, content)
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

    try:
        content = await file.read()
        result = generate_xai(model, content, target_class=target_class)
        if "error" in result:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=result["error"])
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))
