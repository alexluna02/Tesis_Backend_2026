"""Consultas a expertos.

Un usuario (normalmente un agricultor) conversa en un chat privado con un experto registrado.
  • Chat general (tipo 'general'): una conversación abierta por usuario y experto. El análisis se
    comparte como documento PDF (el reporte que la app ya exporta), además de texto e imágenes.
  • Consultas sobre un análisis (tipos 'individual' y 'parcela'): llevan una foto del análisis y
    el experto puede emitir un dictamen.
El frontend usa polling (no hay WebSockets):
  • GET /consultas/resumen            → campana de notificaciones (cada ~30 s); también sirve de
                                         señal de actividad para mostrar «En línea»
  • GET /consultas/{id}/mensajes      → chat abierto (cada ~5 s, con ?despues=)

Reglas de acceso: solo los dos participantes ven una consulta y sus documentos; para cualquier
otro usuario la consulta «no existe» (404). Todas las marcas de tiempo se escriben aquí en UTC
naive (la convención de la base de datos), así mensajes y lecturas son comparables entre sí.
"""
import base64
import binascii
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import auth
import database
import schemas

router = APIRouter(tags=["consultas"])

C = database.Consulta
M = database.MensajeConsulta
U = database.Usuario

LISTA_MAX = 200
MENSAJES_MAX = 500
VISTA_PREVIA = 140
IMAGEN_MAX_BYTES = 2 * 1024 * 1024
DOCUMENTO_MAX_BYTES = 10 * 1024 * 1024            # el PDF del análisis incluye la foto original
EN_LINEA = timedelta(seconds=90)                  # el frontend da señal cada ~30 s
LATIDO_MIN = timedelta(seconds=20)                # no se escribe la actividad más seguido que esto
_DATA_URL = re.compile(r"^data:image/(jpeg|png|webp);base64,([A-Za-z0-9+/]+={0,2})$")
_FIRMAS = {"jpeg": b"\xff\xd8\xff", "png": b"\x89PNG\r\n\x1a\n", "webp": b"RIFF"}
_SANAS = {"Fondo", "Fondo_y_Sana"}
_CARACTERES_NO_VALIDOS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


def _ahora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _validar_imagen(data_url: str) -> str:
    """Solo JPG, PNG o WEBP en base64 válido, ≤ 2 MB, y con la firma del formato declarado
    (así no se cuela SVG ni otro contenido disfrazado de imagen)."""
    match = _DATA_URL.match(data_url)
    if match is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La imagen debe ser JPG, PNG o WEBP.")
    formato, contenido = match.groups()
    try:
        datos = base64.b64decode(contenido, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="La imagen no es válida.")
    if len(datos) > IMAGEN_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="La imagen supera los 2 MB.")
    firma_ok = datos.startswith(_FIRMAS[formato]) and (formato != "webp" or datos[8:12] == b"WEBP")
    if not firma_ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El contenido no corresponde a una imagen JPG, PNG o WEBP.")
    return data_url


def _texto_limpio(texto: Optional[str]) -> Optional[str]:
    texto = (texto or "").strip()
    return texto or None


def _nombre_documento(nombre: Optional[str]) -> str:
    """Nombre seguro para mostrar y descargar: sin rutas ni caracteres de control, ≤ 120 caracteres
    y con extensión .pdf."""
    base = (nombre or "").replace("\\", "/").rsplit("/", 1)[-1]
    # Los navegadores envían « " », CR y LF del nombre como %22, %0D y %0A: también se quitan
    for codificado in ("%22", "%0D", "%0A", "%0d", "%0a"):
        base = base.replace(codificado, "")
    base = _CARACTERES_NO_VALIDOS.sub("", base).strip().strip(".").strip()
    if not base.lower().endswith(".pdf"):
        base = f"{base or 'documento'}.pdf"
    if len(base) > 120:
        base = base[:-4][:116].rstrip() + ".pdf"
    return base


def _en_linea(user: database.Usuario) -> bool:
    return user.ultima_actividad is not None and _ahora() - user.ultima_actividad <= EN_LINEA


def _registrar_actividad(user: database.Usuario) -> bool:
    """Marca al usuario como activo ahora (sin commit). Devuelve True si cambió algo."""
    ahora = _ahora()
    if user.ultima_actividad is None or ahora - user.ultima_actividad >= LATIDO_MIN:
        user.ultima_actividad = ahora
        return True
    return False


async def _consulta_de(db: AsyncSession, consulta_id: UUID, user: database.Usuario) -> database.Consulta:
    """La consulta, solo si el usuario es uno de sus dos participantes (si no, 404)."""
    consulta = await db.get(C, consulta_id)
    if consulta is None or user.id not in (consulta.solicitante_id, consulta.experto_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Consulta no encontrada")
    return consulta


def _soy_experto(consulta: database.Consulta, user: database.Usuario) -> bool:
    return consulta.experto_id == user.id


def _mi_lectura(consulta: database.Consulta, user: database.Usuario) -> Optional[datetime]:
    return consulta.leido_experto_at if _soy_experto(consulta, user) else consulta.leido_solicitante_at


def _marcar_leido(consulta: database.Consulta, user: database.Usuario, cuando: datetime) -> None:
    if _soy_experto(consulta, user):
        consulta.leido_experto_at = cuando
    else:
        consulta.leido_solicitante_at = cuando


def _clase_dominante(muestras: list) -> str:
    """La enfermedad con más zonas afectadas de la visita, o «Fondo» si todas están sanas."""
    clases = [
        ("Mancha_blanca" if m.get("clase") == "Mancha_Blanca" else m.get("clase"))
        for m in muestras
        if m.get("clase") and m.get("clase") not in _SANAS
    ]
    return Counter(clases).most_common(1)[0][0] if clases else "Fondo"


def _resumen_corto(consulta: database.Consulta) -> dict:
    datos = consulta.resumen or {}
    if consulta.tipo == "general":
        return {}
    if consulta.tipo == "individual":
        claves = ("clase", "clase_idx", "afectacion_pct", "severidad", "fecha")
    else:
        claves = ("parcela_nombre", "hectareas", "fecha", "afectacion_promedio", "zonas_afectadas", "zonas_total", "clase_dominante")
    return {clave: datos.get(clave) for clave in claves}


def _participante(user: database.Usuario) -> schemas.ParticipanteRead:
    return schemas.ParticipanteRead(
        id=user.id, nombre=user.nombre, rol=user.rol, especialidad=user.especialidad,
        en_linea=_en_linea(user), ultima_actividad=user.ultima_actividad,
    )


def _dictamen(consulta: database.Consulta) -> Optional[schemas.DictamenRead]:
    if consulta.dictamen_fecha is None:
        return None
    return schemas.DictamenRead(
        coincide=bool(consulta.dictamen_coincide),
        diagnostico=consulta.dictamen_diagnostico,
        texto=consulta.dictamen_texto,
        fecha=consulta.dictamen_fecha,
    )


def _dictamen_nuevo(consulta: database.Consulta, user: database.Usuario) -> bool:
    if _soy_experto(consulta, user) or consulta.dictamen_fecha is None:
        return False
    return consulta.leido_solicitante_at is None or consulta.dictamen_fecha > consulta.leido_solicitante_at


def _filtro_no_leidos(user: database.Usuario):
    """Mensajes del otro participante posteriores a mi última lectura (requiere unir con consultas)."""
    mi_lectura = case((C.solicitante_id == user.id, C.leido_solicitante_at), else_=C.leido_experto_at)
    return (M.autor_id != user.id) & or_(mi_lectura.is_(None), M.created_at > mi_lectura)


async def _no_leidos_por_consulta(db: AsyncSession, user: database.Usuario, ids: List) -> dict:
    if not ids:
        return {}
    filas = (await db.execute(
        select(M.consulta_id, func.count())
        .join(C, C.id == M.consulta_id)
        .where(M.consulta_id.in_(ids), _filtro_no_leidos(user))
        .group_by(M.consulta_id)
    )).all()
    return {consulta_id: n for consulta_id, n in filas}


async def _ultimos_mensajes(db: AsyncSession, ids: List) -> dict:
    """Último mensaje de cada consulta, sin cargar las imágenes (solo si tiene)."""
    if not ids:
        return {}
    filas = (await db.execute(
        select(
            M.consulta_id,
            func.left(M.texto, VISTA_PREVIA).label("texto"),
            M.imagen_url.is_not(None).label("tiene_imagen"),
            M.documento_nombre,
            M.autor_id,
            M.created_at,
        )
        .where(M.consulta_id.in_(ids))
        .distinct(M.consulta_id)
        .order_by(M.consulta_id, M.created_at.desc(), M.id.desc())
    )).all()
    return {
        f.consulta_id: schemas.UltimoMensajeRead(
            texto=f.texto, tiene_imagen=f.tiene_imagen, documento_nombre=f.documento_nombre,
            autor_id=f.autor_id, created_at=f.created_at,
        )
        for f in filas
    }


def _item(consulta, user, solicitante, experto, no_leidos: int, ultimo) -> dict:
    soy_experto = _soy_experto(consulta, user)
    return dict(
        id=consulta.id,
        tipo=consulta.tipo,
        estado=consulta.estado,
        created_at=consulta.created_at,
        ultimo_mensaje_at=consulta.ultimo_mensaje_at,
        rol_en_consulta="experto" if soy_experto else "solicitante",
        contraparte=_participante(solicitante if soy_experto else experto),
        resumen=_resumen_corto(consulta),
        ultimo_mensaje=ultimo,
        no_leidos=no_leidos,
        nueva=soy_experto and consulta.leido_experto_at is None,
        dictamen=_dictamen(consulta),
        dictamen_nuevo=_dictamen_nuevo(consulta, user),
    )


async def _detalle(db: AsyncSession, consulta: database.Consulta, user: database.Usuario, ya_existia: bool = False) -> schemas.ConsultaDetalle:
    solicitante = await db.get(U, consulta.solicitante_id)
    experto = await db.get(U, consulta.experto_id)
    no_leidos = (await _no_leidos_por_consulta(db, user, [consulta.id])).get(consulta.id, 0)
    ultimo = (await _ultimos_mensajes(db, [consulta.id])).get(consulta.id)

    analisis = None
    if consulta.analisis_id is not None:
        fila = (await db.execute(
            select(database.Analisis)
            .options(selectinload(database.Analisis.enfermedad))
            .where(database.Analisis.id == consulta.analisis_id)
        )).scalars().first()
        if fila is not None:
            analisis = schemas.AnalisisConsultaRead(
                id=fila.id,
                imagen_url=fila.imagen_url,
                grad_cam_url=fila.grad_cam_url,
                resultado_url=fila.resultado_url,
                fecha=fila.fecha,
                afectacion_pct=fila.afectacion_pct,
                severidad=fila.severidad,
                clase=fila.enfermedad.nombre,
                clase_idx=fila.enfermedad.clase_idx,
            )

    soy_experto = _soy_experto(consulta, user)
    return schemas.ConsultaDetalle(
        **_item(consulta, user, solicitante, experto, no_leidos, ultimo),
        solicitante=_participante(solicitante),
        experto=_participante(experto),
        snapshot=consulta.resumen or {},
        analisis=analisis,
        leido_contraparte_at=consulta.leido_solicitante_at if soy_experto else consulta.leido_experto_at,
        puede_escribir=consulta.estado == "abierta",
        soy_experto=soy_experto,
        cerrada_at=consulta.cerrada_at,
        ya_existia=ya_existia,
    )


async def _agregar_mensaje(
    db: AsyncSession,
    consulta: database.Consulta,
    user: database.Usuario,
    texto: Optional[str],
    imagen_url: Optional[str],
    client_id: Optional[str],
    documento: Optional[tuple[str, bytes]] = None,
) -> tuple[database.MensajeConsulta, bool]:
    """Agrega un mensaje (sin commit). Si ya existe uno con el mismo client_id (reintento del mismo
    envío), devuelve ese y no crea otro. `documento` = (nombre, bytes del PDF). Devuelve (mensaje, es_nuevo)."""
    if client_id:
        existente = await _mensaje_por_client_id(db, consulta.id, user.id, client_id)
        if existente is not None:
            return existente, False
    ahora = _ahora()
    mensaje = M(consulta_id=consulta.id, autor_id=user.id, texto=texto, imagen_url=imagen_url, client_id=client_id, created_at=ahora)
    if documento is not None:
        mensaje.documento_nombre, mensaje.documento_datos = documento
        mensaje.documento_tamano = len(documento[1])
    db.add(mensaje)
    consulta.ultimo_mensaje_at = ahora
    _marcar_leido(consulta, user, ahora)   # quien escribe ya vio todo lo anterior
    return mensaje, True


async def _mensaje_por_client_id(db: AsyncSession, consulta_id, autor_id, client_id: str) -> Optional[database.MensajeConsulta]:
    return (await db.execute(
        select(M).where(M.consulta_id == consulta_id, M.autor_id == autor_id, M.client_id == client_id)
    )).scalars().first()


async def _consulta_abierta_igual(db: AsyncSession, user, experto_id, analisis_id, referencia, tipo: str) -> Optional[database.Consulta]:
    if tipo == "general":
        filtro = C.tipo == "general"
    elif analisis_id is not None:
        filtro = C.analisis_id == analisis_id
    else:
        filtro = C.referencia == referencia
    return (await db.execute(
        select(C).where(C.solicitante_id == user.id, C.experto_id == experto_id, C.estado == "abierta", filtro)
    )).scalars().first()


# ── Expertos disponibles ──────────────────────────────────────────────────────
@router.get("/expertos", response_model=List[schemas.ExpertoRead])
async def listar_expertos(
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    abiertas = (
        select(C.experto_id, func.count().label("n"))
        .where(C.estado == "abierta")
        .group_by(C.experto_id)
        .subquery()
    )
    filas = (await db.execute(
        select(U, func.coalesce(abiertas.c.n, 0))
        .outerjoin(abiertas, abiertas.c.experto_id == U.id)
        .where(U.rol == "experto", U.activo.is_(True), U.id != user.id)
        .order_by(U.nombre)
    )).all()
    return [
        schemas.ExpertoRead(
            id=e.id, nombre=e.nombre, consultas_abiertas=k, especialidad=e.especialidad,
            en_linea=_en_linea(e), ultima_actividad=e.ultima_actividad,
        )
        for e, k in filas
    ]


# ── Crear y listar consultas ──────────────────────────────────────────────────
@router.post("/consultas", response_model=schemas.ConsultaDetalle, status_code=status.HTTP_201_CREATED)
async def crear_consulta(
    req: schemas.ConsultaCreate,
    response: Response,
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    if req.experto_id == user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No puedes enviarte una consulta a ti mismo.")
    experto = await db.get(U, req.experto_id)
    if experto is None or experto.rol != "experto" or not experto.activo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Experto no encontrado")
    experto_id = experto.id   # un rollback expira los objetos de la sesión: se guardan los ids antes

    if req.tipo == "individual":
        if req.analisis_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Falta el análisis que quieres consultar.")
        analisis = (await db.execute(
            select(database.Analisis)
            .options(selectinload(database.Analisis.enfermedad))
            .where(database.Analisis.id == req.analisis_id, database.Analisis.user_id == user.id)
        )).scalars().first()
        if analisis is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Análisis no encontrado")
        # El resumen de un análisis individual lo arma el servidor: nunca se confía en el cliente
        snapshot = {
            "tipo": "individual",
            "clase": analisis.enfermedad.nombre,
            "clase_idx": analisis.enfermedad.clase_idx,
            "afectacion_pct": float(analisis.afectacion_pct),
            "severidad": analisis.severidad,
            "fecha": analisis.fecha.isoformat(),
        }
        analisis_id, referencia = analisis.id, None
    elif req.tipo == "general":
        # Chat libre: el análisis se comparte después como documento PDF dentro de la conversación
        snapshot = {"tipo": "general"}
        analisis_id, referencia = None, None
    else:
        if req.parcela is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Faltan los datos de la parcela.")
        datos = req.parcela.model_dump(mode="json")
        snapshot = {"tipo": "parcela", **datos, "clase_dominante": _clase_dominante(datos["muestras"])}
        analisis_id, referencia = None, req.referencia

    mensaje = _texto_limpio(req.mensaje)

    async def devolver_existente(existente: database.Consulta) -> schemas.ConsultaDetalle:
        # Mismo análisis y mismo experto con una consulta abierta: se continúa esa conversación
        if mensaje:
            await _agregar_mensaje(db, existente, user, mensaje, None, req.client_id)
            await db.commit()
        response.status_code = status.HTTP_200_OK
        return await _detalle(db, existente, user, ya_existia=True)

    if req.tipo == "general" or analisis_id is not None or referencia is not None:
        existente = await _consulta_abierta_igual(db, user, experto_id, analisis_id, referencia, req.tipo)
        if existente is not None:
            return await devolver_existente(existente)

    ahora = _ahora()
    consulta = C(
        solicitante_id=user.id,
        experto_id=experto_id,
        tipo=req.tipo,
        analisis_id=analisis_id,
        referencia=referencia,
        resumen=snapshot,
        estado="abierta",
        leido_solicitante_at=ahora,
        ultimo_mensaje_at=ahora,
        created_at=ahora,
    )
    db.add(consulta)
    try:
        await db.flush()
    except IntegrityError:
        # Carrera (doble clic): otra petición acaba de crear la misma consulta
        await db.rollback()
        await db.refresh(user)    # el rollback lo expiró; en async no se puede recargar de forma implícita
        existente = await _consulta_abierta_igual(db, user, experto_id, analisis_id, referencia, req.tipo)
        if existente is None:
            raise
        return await devolver_existente(existente)

    if mensaje:
        await _agregar_mensaje(db, consulta, user, mensaje, None, req.client_id)
    await db.commit()
    return await _detalle(db, consulta, user)


@router.get("/consultas", response_model=List[schemas.ConsultaItem])
async def listar_consultas(
    estado: Optional[str] = Query(None, pattern="^(abierta|cerrada)$"),
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    query = (
        select(C)
        .where(or_(C.solicitante_id == user.id, C.experto_id == user.id))
        .order_by(C.ultimo_mensaje_at.desc())
        .limit(LISTA_MAX)
    )
    if estado:
        query = query.where(C.estado == estado)
    consultas = (await db.execute(query)).scalars().all()
    if not consultas:
        return []
    ids = [c.id for c in consultas]
    usuarios_ids = {c.solicitante_id for c in consultas} | {c.experto_id for c in consultas}
    usuarios = {u.id: u for u in (await db.execute(select(U).where(U.id.in_(usuarios_ids)))).scalars().all()}
    no_leidos = await _no_leidos_por_consulta(db, user, ids)
    ultimos = await _ultimos_mensajes(db, ids)
    return [
        schemas.ConsultaItem(**_item(c, user, usuarios[c.solicitante_id], usuarios[c.experto_id], no_leidos.get(c.id, 0), ultimos.get(c.id)))
        for c in consultas
    ]


# Debe declararse antes de /consultas/{consulta_id}
@router.get("/consultas/resumen", response_model=schemas.ConsultasResumen)
async def resumen_consultas(
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    # El frontend pide el resumen cada ~30 s mientras la app está visible: es la señal de «En línea»
    if _registrar_actividad(user):
        await db.commit()
    mias = or_(C.solicitante_id == user.id, C.experto_id == user.id)
    con_mensajes = (await db.execute(
        select(M.consulta_id, func.count())
        .join(C, C.id == M.consulta_id)
        .where(mias, _filtro_no_leidos(user))
        .group_by(M.consulta_id)
    )).all()
    no_leidos = sum(n for _, n in con_mensajes)
    novedad = {consulta_id for consulta_id, _ in con_mensajes}

    filas = (await db.execute(
        select(C.id, C.experto_id, C.leido_experto_at, C.leido_solicitante_at, C.dictamen_fecha).where(mias)
    )).all()
    nuevas = 0
    for f in filas:
        if f.experto_id == user.id:
            if f.leido_experto_at is None:
                nuevas += 1
                novedad.add(f.id)
        elif f.dictamen_fecha is not None and (f.leido_solicitante_at is None or f.dictamen_fecha > f.leido_solicitante_at):
            novedad.add(f.id)
    return schemas.ConsultasResumen(no_leidos=no_leidos, consultas_con_novedad=len(novedad), nuevas=nuevas)


@router.get("/consultas/{consulta_id}", response_model=schemas.ConsultaDetalle)
async def obtener_consulta(
    consulta_id: UUID,
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    consulta = await _consulta_de(db, consulta_id, user)
    return await _detalle(db, consulta, user)


# ── Chat ──────────────────────────────────────────────────────────────────────
@router.get("/consultas/{consulta_id}/mensajes", response_model=schemas.MensajesRespuesta)
async def listar_mensajes(
    consulta_id: UUID,
    despues: Optional[datetime] = Query(None, description="Solo mensajes posteriores a esta fecha (UTC). Para el polling, pide con algunos segundos de margen y descarta los repetidos por id."),
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    consulta = await _consulta_de(db, consulta_id, user)
    query = select(M).where(M.consulta_id == consulta.id)
    despues = _utc_naive(despues)
    if despues is not None:
        query = query.where(M.created_at > despues)
    recientes = (await db.execute(query.order_by(M.created_at.desc(), M.id.desc()).limit(MENSAJES_MAX))).scalars().all()
    return schemas.MensajesRespuesta(
        mensajes=list(reversed(recientes)),
        estado=consulta.estado,
        leido_contraparte_at=consulta.leido_solicitante_at if _soy_experto(consulta, user) else consulta.leido_experto_at,
        dictamen_fecha=consulta.dictamen_fecha,
        ultimo_mensaje_at=consulta.ultimo_mensaje_at,
    )


@router.post("/consultas/{consulta_id}/mensajes", response_model=schemas.MensajeRead, status_code=status.HTTP_201_CREATED)
async def enviar_mensaje(
    consulta_id: UUID,
    req: schemas.MensajeCreate,
    response: Response,
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    consulta = await _consulta_de(db, consulta_id, user)
    if consulta.estado != "abierta":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La consulta está cerrada. Reábrela para seguir escribiendo.")
    texto = _texto_limpio(req.texto)
    imagen = _validar_imagen(req.imagen_url) if req.imagen_url else None
    if texto is None and imagen is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Escribe un mensaje o adjunta una imagen.")
    try:
        mensaje, nuevo = await _agregar_mensaje(db, consulta, user, texto, imagen, req.client_id)
        await db.commit()
    except IntegrityError:
        # Carrera entre dos reintentos con el mismo client_id: se devuelve el que quedó guardado
        await db.rollback()
        await db.refresh(user)
        mensaje = (await db.execute(
            select(M).where(M.consulta_id == consulta_id, M.autor_id == user.id, M.client_id == req.client_id)
        )).scalars().first()
        if mensaje is None:
            raise
        nuevo = False
    if not nuevo:
        response.status_code = status.HTTP_200_OK
    return mensaje


@router.post("/consultas/{consulta_id}/documentos", response_model=schemas.MensajeRead, status_code=status.HTTP_201_CREATED)
async def enviar_documento(
    consulta_id: UUID,
    response: Response,
    archivo: UploadFile = File(..., description="Documento PDF (≤ 10 MB), p. ej. el reporte de un análisis"),
    texto: Optional[str] = Form(None, max_length=4000),
    client_id: Optional[str] = Form(None, min_length=1, max_length=64),
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    """Envía un PDF al chat (multipart/form-data), con un texto opcional."""
    consulta = await _consulta_de(db, consulta_id, user)
    if consulta.estado != "abierta":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La consulta está cerrada. Reábrela para seguir escribiendo.")
    if client_id:
        # Reintento del mismo envío: se devuelve el mensaje ya guardado sin volver a procesar el archivo
        existente = await _mensaje_por_client_id(db, consulta.id, user.id, client_id)
        if existente is not None:
            response.status_code = status.HTTP_200_OK
            return existente
    datos = await archivo.read(DOCUMENTO_MAX_BYTES + 1)
    if len(datos) > DOCUMENTO_MAX_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="El documento supera los 10 MB.")
    if not datos.startswith(b"%PDF-"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Solo se pueden enviar documentos PDF.")
    documento = (_nombre_documento(archivo.filename), datos)
    try:
        mensaje, nuevo = await _agregar_mensaje(db, consulta, user, _texto_limpio(texto), None, client_id, documento)
        await db.commit()
    except IntegrityError:
        # Carrera entre dos reintentos con el mismo client_id: se devuelve el que quedó guardado
        await db.rollback()
        await db.refresh(user)
        mensaje = await _mensaje_por_client_id(db, consulta_id, user.id, client_id) if client_id else None
        if mensaje is None:
            raise
        nuevo = False
    if not nuevo:
        response.status_code = status.HTTP_200_OK
    return mensaje


@router.get(
    "/consultas/{consulta_id}/mensajes/{mensaje_id}/documento",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}, "description": "El PDF adjunto"}},
)
async def descargar_documento(
    consulta_id: UUID,
    mensaje_id: UUID,
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    consulta = await _consulta_de(db, consulta_id, user)
    fila = (await db.execute(
        select(M.documento_nombre, M.documento_datos).where(M.id == mensaje_id, M.consulta_id == consulta.id)
    )).first()
    if fila is None or fila.documento_datos is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Documento no encontrado")
    nombre = fila.documento_nombre
    ascii_nombre = nombre.encode("ascii", "ignore").decode().replace('"', "") or "documento.pdf"
    return Response(
        content=fila.documento_datos,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_nombre}\"; filename*=UTF-8''{quote(nombre)}",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/consultas/{consulta_id}/leer", status_code=status.HTTP_204_NO_CONTENT)
async def marcar_leida(
    consulta_id: UUID,
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    consulta = await _consulta_de(db, consulta_id, user)
    _marcar_leido(consulta, user, _ahora())
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Dictamen y estado ─────────────────────────────────────────────────────────
@router.put("/consultas/{consulta_id}/dictamen", response_model=schemas.ConsultaDetalle)
async def guardar_dictamen(
    consulta_id: UUID,
    req: schemas.DictamenUpdate,
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    consulta = await _consulta_de(db, consulta_id, user)
    # Solo el experto asignado, y solo mientras conserve el rol de experto
    if consulta.experto_id != user.id or user.rol != "experto":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Solo el experto asignado puede emitir el dictamen.")
    if consulta.estado != "abierta":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="La consulta está cerrada. Reábrela para cambiar el dictamen.")
    texto = _texto_limpio(req.texto)
    if texto is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Escribe la recomendación del dictamen.")
    ahora = _ahora()
    consulta.dictamen_coincide = req.coincide
    consulta.dictamen_diagnostico = req.diagnostico
    consulta.dictamen_texto = texto
    consulta.dictamen_fecha = ahora
    consulta.ultimo_mensaje_at = ahora        # la consulta sube en la lista del solicitante
    consulta.leido_experto_at = ahora
    await db.commit()
    return await _detalle(db, consulta, user)


@router.patch("/consultas/{consulta_id}/estado", response_model=schemas.ConsultaDetalle)
async def cambiar_estado(
    consulta_id: UUID,
    req: schemas.EstadoConsultaUpdate,
    user: database.Usuario = Depends(auth.get_current_active_user),
    db: AsyncSession = Depends(database.get_db),
):
    consulta = await _consulta_de(db, consulta_id, user)
    if consulta.estado != req.estado:
        consulta.estado = req.estado
        consulta.cerrada_at = _ahora() if req.estado == "cerrada" else None
        try:
            await db.commit()
        except IntegrityError:
            # Reabrir chocaría con otra consulta abierta sobre el mismo análisis y experto
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Ya tienes otra consulta abierta con este experto sobre el mismo análisis.",
            )
    return await _detalle(db, consulta, user)
