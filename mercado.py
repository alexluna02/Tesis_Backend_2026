"""Precios del mercado agrícola: maíz (grano seco) y choclo.

Fuente diaria — choclo: boletín público de precios de la Empresa Pública
Municipal Mercado Mayorista Ambato (EP-EMA), publicado como PDF cada día hábil
en https://ambato-ema.gob.ec/listaprecios/. Se descarga y parsea ese PDF para
extraer las filas cuyo producto contiene "choclo" (la variedad reportada
cambia de un día a otro, así que se promedia si hay más de una).

Fuente de temporada — maíz duro: no existe un mercado que lo cotice a diario
(es un commodity de grano seco, no un perecible); el precio lo fija por
cosecha el Consejo Consultivo de la Cadena de Maíz Amarillo Duro y lo publica
el SIPA (Ministerio de Agricultura). Se guarda como una banda de referencia,
no como una serie diaria.
"""
import asyncio
import io
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import pdfplumber
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import database

AMBATO_URL_TEMPLATE = "https://ambato-ema.gob.ec/wp-content/uploads/%Y/%m/precios-%Y-%m-%d.pdf"
FUENTE_CHOCLO = "EP-EMA · Mercado Mayorista Ambato"
FUENTE_MAIZ_DURO = "SIPA - MAG · Consejo Consultivo Cadena Maíz Amarillo Duro"

# Banda oficial vigente para la temporada 2026 (piso/techo/promedio referencial,
# grano limpio y seco al 13% de humedad, entregado en bodega del comprador).
MAIZ_DURO_REFERENCIA = {
    "precio_min": 17.45,
    "precio_max": 19.61,
    "precio": 18.53,
    "presentacion": "Quintal (100 lb) — grano seco, 13% humedad",
    "temporada": "2026",
}

_HTTP_TIMEOUT = httpx.Timeout(15.0)


def _to_float(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", "."))
    except (TypeError, ValueError):
        return None


def _parse_row(raw: str) -> Optional[dict]:
    tokens = raw.split()
    if len(tokens) < 4 or not tokens[0].isdigit():
        return None

    precio = _to_float(tokens[-1])
    if precio is None:
        return None

    peso = _to_float(tokens[-2]) if len(tokens) >= 5 else None
    presentacion_idx = -3 if peso is not None else -2
    presentacion = tokens[presentacion_idx]
    producto = " ".join(tokens[1:presentacion_idx])
    if not producto:
        return None

    return {"producto": producto, "presentacion": presentacion, "peso": peso, "precio": precio}


def _extract_matching_rows(pdf_bytes: bytes, keyword: str) -> list[dict]:
    matches = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    if not row:
                        continue
                    raw = " ".join(str(c) for c in row if c)
                    parsed = _parse_row(raw)
                    if parsed and keyword in parsed["producto"].lower():
                        matches.append(parsed)
    return matches


async def _fetch_pdf(client: httpx.AsyncClient, fecha: datetime) -> Optional[bytes]:
    url = fecha.strftime(AMBATO_URL_TEMPLATE)
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200 or not resp.content:
        return None
    return resp.content


def _aggregate(rows: list[dict]) -> dict:
    precios = [r["precio"] for r in rows]
    con_peso = next((r for r in rows if r.get("peso")), None)
    unidad = f"{con_peso['presentacion']} ({con_peso['peso']:g} kg)" if con_peso else rows[0]["presentacion"]
    variedades = ", ".join(f"{r['producto']} (${r['precio']:g})" for r in rows)
    return {
        "precio": round(sum(precios) / len(precios), 2),
        "precio_min": min(precios),
        "precio_max": max(precios),
        "presentacion": unidad,
        "variedades": variedades,
    }


def _hoy() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


async def sync_choclo_prices(db: AsyncSession, days_back: int = 14) -> int:
    """Rellena los días recientes sin precio de choclo consultando el PDF diario de EP-EMA."""
    candidatos = [_hoy() - timedelta(days=i) for i in range(days_back)]

    existentes = await db.execute(
        select(database.PrecioMercado.fecha).where(
            database.PrecioMercado.producto == "choclo",
            database.PrecioMercado.fecha.in_(candidatos),
        )
    )
    fechas_existentes = {row[0] for row in existentes.all()}
    pendientes = [f for f in candidatos if f not in fechas_existentes]
    if not pendientes:
        return 0

    insertados = 0
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        for fecha in pendientes:
            pdf_bytes = await _fetch_pdf(client, fecha)
            if pdf_bytes is None:
                continue
            try:
                rows = await asyncio.to_thread(_extract_matching_rows, pdf_bytes, "choclo")
            except Exception:
                continue
            if not rows:
                continue
            agg = _aggregate(rows)
            db.add(database.PrecioMercado(
                producto="choclo",
                fuente=FUENTE_CHOCLO,
                presentacion=agg["presentacion"],
                precio=agg["precio"],
                precio_min=agg["precio_min"],
                precio_max=agg["precio_max"],
                variedades=agg["variedades"],
                fecha=fecha,
            ))
            insertados += 1

    if insertados:
        await db.commit()
    return insertados


async def ensure_maiz_duro_seed(db: AsyncSession) -> None:
    """Crea la banda de referencia de maíz duro si todavía no existe ninguna."""
    existente = await db.execute(
        select(database.PrecioMercado).where(database.PrecioMercado.producto == "maiz_duro")
    )
    if existente.scalars().first() is not None:
        return
    db.add(database.PrecioMercado(
        producto="maiz_duro",
        fuente=FUENTE_MAIZ_DURO,
        presentacion=MAIZ_DURO_REFERENCIA["presentacion"],
        precio=MAIZ_DURO_REFERENCIA["precio"],
        precio_min=MAIZ_DURO_REFERENCIA["precio_min"],
        precio_max=MAIZ_DURO_REFERENCIA["precio_max"],
        variedades=f"Banda oficial temporada {MAIZ_DURO_REFERENCIA['temporada']}",
        fecha=_hoy(),
    ))
    await db.commit()


def _tendencia(actual: float, anterior: Optional[float]) -> tuple[str, Optional[float], Optional[float]]:
    if anterior is None or anterior == 0:
        return "estable", None, None
    diff = round(actual - anterior, 2)
    pct = round((diff / anterior) * 100, 2)
    if abs(pct) < 0.5:
        return "estable", diff, pct
    return ("sube" if diff > 0 else "baja"), diff, pct


async def get_resumen(db: AsyncSession) -> dict:
    resultado = {"choclo": None, "maiz_duro": None}

    choclo_rows = (await db.execute(
        select(database.PrecioMercado)
        .where(database.PrecioMercado.producto == "choclo")
        .order_by(database.PrecioMercado.fecha.desc())
        .limit(2)
    )).scalars().all()

    if choclo_rows:
        actual = choclo_rows[0]
        anterior = choclo_rows[1] if len(choclo_rows) > 1 else None
        tendencia, diff, pct = _tendencia(float(actual.precio), float(anterior.precio) if anterior else None)
        resultado["choclo"] = {
            "precio": actual.precio,
            "precio_min": actual.precio_min,
            "precio_max": actual.precio_max,
            "unidad": actual.presentacion,
            "fecha": actual.fecha,
            "fuente": actual.fuente,
            "variedades": actual.variedades,
            "tendencia": tendencia,
            "variacion_abs": diff,
            "variacion_pct": pct,
        }

    maiz_row = (await db.execute(
        select(database.PrecioMercado)
        .where(database.PrecioMercado.producto == "maiz_duro")
        .order_by(database.PrecioMercado.fecha.desc())
        .limit(1)
    )).scalars().first()

    if maiz_row:
        resultado["maiz_duro"] = {
            "precio_promedio": maiz_row.precio,
            "precio_min": maiz_row.precio_min,
            "precio_max": maiz_row.precio_max,
            "unidad": maiz_row.presentacion,
            "fuente": maiz_row.fuente,
            "temporada": MAIZ_DURO_REFERENCIA["temporada"],
            "actualizado": maiz_row.fecha,
        }

    return resultado


async def get_historico(db: AsyncSession, producto: str, dias: int) -> list[database.PrecioMercado]:
    desde = _hoy() - timedelta(days=dias)
    result = await db.execute(
        select(database.PrecioMercado)
        .where(database.PrecioMercado.producto == producto, database.PrecioMercado.fecha >= desde)
        .order_by(database.PrecioMercado.fecha.asc())
    )
    return result.scalars().all()
