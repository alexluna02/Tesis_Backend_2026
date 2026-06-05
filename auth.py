import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

import bcrypt as _bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import database
import schemas

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "cambia_esta_llave_secreta")
ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return _bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))


def get_password_hash(password: str) -> str:
    return _bcrypt.hashpw(password.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token() -> str:
    return secrets.token_urlsafe(32)


async def authenticate_user(db: AsyncSession, email: str, password: str) -> Optional[database.Usuario]:
    result = await db.execute(select(database.Usuario).where(database.Usuario.email == email))
    user = result.scalars().first()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(database.get_db),
) -> database.Usuario:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudo validar las credenciales",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = payload.get("sub")
        email = payload.get("email")
        if user_id is None or email is None:
            raise credentials_exception
        token_data = schemas.TokenPayload(sub=user_id, email=email, exp=payload.get("exp"))
    except JWTError:
        raise credentials_exception

    result = await db.execute(select(database.Usuario).where(database.Usuario.id == token_data.sub))
    user = result.scalars().first()
    if user is None:
        raise credentials_exception
    return user


async def get_current_active_user(current_user: database.Usuario = Depends(get_current_user)) -> database.Usuario:
    if not current_user.activo:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Usuario inactivo")
    return current_user


async def get_current_active_admin(current_user: database.Usuario = Depends(get_current_active_user)) -> database.Usuario:
    if current_user.rol != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acceso de administrador requerido")
    return current_user


async def rotate_refresh_token(db: AsyncSession, current_token: str) -> tuple[database.Usuario, str]:
    result = await db.execute(
        select(database.RefreshToken)
        .where(
            database.RefreshToken.token == current_token,
            database.RefreshToken.revocado == False,
            database.RefreshToken.expires_at > datetime.utcnow(),
        )
    )
    token_row = result.scalars().first()
    if token_row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token inválido o expirado")

    user = token_row.user
    token_row.revocado = True
    await db.execute(
        update(database.RefreshToken)
        .where(database.RefreshToken.id == token_row.id)
        .values(revocado=True)
    )

    new_refresh_token = create_refresh_token()
    new_token = database.RefreshToken(
        user_id=token_row.user_id,
        token=new_refresh_token,
        expires_at=datetime.utcnow() + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
    )
    db.add(new_token)
    await db.commit()
    return user, new_refresh_token


async def revoke_refresh_token(db: AsyncSession, refresh_token: str) -> None:
    result = await db.execute(
        select(database.RefreshToken)
        .where(database.RefreshToken.token == refresh_token, database.RefreshToken.revocado == False)
    )
    token_row = result.scalars().first()
    if token_row is None:
        return
    token_row.revocado = True
    await db.commit()
