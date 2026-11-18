"""Precios del mercado agrícola para AgroScan.

Fuente diaria: boletín público de precios de la Empresa Pública Municipal Mercado Mayorista
Ambato (EP-EMA), un PDF por día hábil enlazado en https://ambato-ema.gob.ec/listaprecios/. Se
descarga una vez por día, se lee cada fila (producto, presentación, peso y precio) y se agrupan
las variedades de los 10 productos que muestra AgroScan. Por producto y día se guarda el promedio
de sus variedades, el mínimo, el máximo y la lista de variedades con su precio.

Fuente de temporada — maíz duro: no existe un mercado que lo cotice a diario (es un commodity de
grano seco, no un perecible); el precio lo fija por cosecha el Consejo Consultivo de la Cadena de
Maíz Amarillo Duro y lo publica el SIPA (Ministerio de Agricultura). Se guarda como una banda de
referencia, no como una serie diaria.
"""
import asyncio
import io
import re
import statistics
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import httpx
import pdfplumber
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

import database

AMBATO_URL_TEMPLATE = "https://ambato-ema.gob.ec/wp-content/uploads/%Y/%m/precios-%Y-%m-%d.pdf"
AMBATO_LISTA_URL = "https://ambato-ema.gob.ec/listaprecios/"
FUENTE_EMA = "EP-EMA · Mercado Mayorista Ambato"
FUENTE_CHOCLO = FUENTE_EMA  # nombre anterior, se mantiene por compatibilidad
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

_HTTP_TIMEOUT = httpx.Timeout(20.0)
_HTTP_HEADERS = {"User-Agent": "AgroScan/1.0 (precios de mercado)"}

# Presentaciones que usa el boletín; la última que aparece en la fila separa el nombre del peso
PRESENTACIONES = {
    "atado", "balde", "bandeja", "bulto", "caja", "cajon", "canasta", "carton", "ciento", "costal",
    "cubeta", "docena", "funda", "gaveta", "jaba", "kilo", "kilogramo", "libra", "malla", "mano",
    "paquete", "quintal", "racimo", "saco", "tarrina", "unidad",
}


def _norm(texto: str) -> str:
    """Minúsculas y sin tildes, para comparar nombres del boletín."""
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()


# Los 10 productos de AgroScan (máximo pedido por el usuario) y qué filas del boletín agrupan.
# Cada regla recibe la fila leída: «n» nombre normalizado, «base» presentación normalizada y
# «peso» en kg. Solo se agrupan variedades con la misma presentación, para que el promedio tenga
# sentido. «choclo» conserva su clave original (filas ya guardadas y endpoint /mercado/resumen).
PRODUCTOS = [
    {"clave": "choclo", "nombre": "Maíz (choclo)", "regla": lambda f: f["n"].startswith("choclo")},
    {"clave": "papa", "nombre": "Papa", "regla": lambda f: f["n"].startswith("papa ") and f["base"] == "quintal"},
    {"clave": "arveja", "nombre": "Arveja tierna", "regla": lambda f: f["n"].startswith("arveja tierna")},
    {"clave": "frejol", "nombre": "Fréjol tierno", "regla": lambda f: f["n"].startswith("frejol") and f["base"] == "saco"},
    {"clave": "haba", "nombre": "Haba tierna", "regla": lambda f: f["n"].startswith("haba")},
    {"clave": "arroz", "nombre": "Arroz", "regla": lambda f: f["n"].startswith("arroz")},
    {"clave": "cebolla", "nombre": "Cebolla colorada", "regla": lambda f: f["n"].startswith("cebolla colorada") and f["base"] == "malla"},
    {"clave": "zanahoria", "nombre": "Zanahoria", "regla": lambda f: f["n"].startswith("zanahoria") and f["base"] == "saco" and (f["peso"] or 0) >= 40},
    {"clave": "tomate_arbol", "nombre": "Tomate de árbol", "regla": lambda f: f["n"].startswith("tomate de arbol") and f["base"] == "saco"},
    {"clave": "aguacate", "nombre": "Aguacate", "regla": lambda f: f["n"].startswith("aguacate") and f["base"] == "saco"},
]
CLAVES = [p["clave"] for p in PRODUCTOS]
NOMBRES = {p["clave"]: p["nombre"] for p in PRODUCTOS}


def _to_float(token: str) -> Optional[float]:
    try:
        return float(token.replace(",", "."))
    except (TypeError, ValueError, AttributeError):
        return None


def _parse_line(raw: str) -> Optional[dict]:
    """Lee una fila del boletín: «Nº Nombre… Presentación [peso] precio».

    El nombre puede seguir en otra línea («… Bola Roja o Saco 50 65⏎Gema») y la presentación puede
    llevar un detalle entre paréntesis («Cubeta (30 Unidades) 5»). El peso falta o es 0 en algunas filas.
    """
    lineas = [l.strip() for l in str(raw).split("\n") if l.strip()]
    if not lineas:
        return None
    primera = lineas[0].split()
    if len(lineas) > 1 and (not primera or _to_float(primera[-1]) is None):
        tokens, resto_nombre = " ".join(lineas).split(), []   # columnas separadas: todo en una fila
    else:
        tokens, resto_nombre = primera, lineas[1:]
    if len(tokens) < 3 or not tokens[0].isdigit():
        return None
    precio = _to_float(tokens[-1])
    if precio is None or precio <= 0:
        return None

    cuerpo = tokens[1:-1]
    idx = None
    for i in range(len(cuerpo) - 1, 0, -1):           # el nombre conserva al menos una palabra
        t = cuerpo[i]
        if t.startswith("(") or t.endswith(")"):       # detalle entre paréntesis
            continue
        if _norm(t) in PRESENTACIONES:
            idx = i
            break
    if idx is None:
        return None

    nombre = " ".join(cuerpo[:idx] + resto_nombre).strip()
    if nombre.isupper():
        nombre = nombre.title()
    peso, detalle = None, []
    for t in cuerpo[idx + 1:]:
        valor = _to_float(t)
        if valor is not None and peso is None and not detalle:
            peso = valor if valor > 0 else None
        else:
            detalle.append(t)
    base = cuerpo[idx]
    presentacion = " ".join([base.capitalize() if base.isupper() else base] + detalle)
    return {"nombre": nombre, "n": _norm(nombre), "presentacion": presentacion, "base": _norm(base),
            "peso": peso, "precio": precio}


def _extract_rows(pdf_bytes: bytes) -> list[dict]:
    """Todas las filas de producto del boletín (sin repetir: algunos PDF traen la tabla dos veces)."""
    filas, vistas = [], set()
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    if not row:
                        continue
                    raw = " ".join(str(c) for c in row if c)
                    if not raw[:1].isdigit():
                        continue
                    fila = _parse_line(raw)
                    if fila is None:
                        continue
                    clave = (fila["n"], fila["base"], fila["peso"])
                    if clave in vistas:
                        continue
                    vistas.add(clave)
                    filas.append(fila)
    return filas


def _aggregate(rows: list[dict]) -> dict:
    precios = [r["precio"] for r in rows]
    con_peso = next((r for r in rows if r.get("peso")), None)
    unidad = f"{con_peso['presentacion']} ({con_peso['peso']:g} kg)" if con_peso else rows[0]["presentacion"]
    variedades = ", ".join(f"{r['nombre']} (${r['precio']:g})" for r in rows)
    return {
        "precio": round(sum(precios) / len(precios), 2),
        "precio_min": min(precios),
        "precio_max": max(precios),
        "presentacion": unidad[:60],
        "variedades": variedades,
    }


async def _fetch_pdf(client: httpx.AsyncClient, fecha: datetime) -> Optional[bytes]:
    """PDF del día o None. Los días sin boletín el sitio responde 404 con una página HTML de
    ~300 KB: se lee solo el estado para no descargarla."""
    url = fecha.strftime(AMBATO_URL_TEMPLATE)
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code != 200 or "pdf" not in resp.headers.get("content-type", "").lower():
                return None
            contenido = await resp.aread()
    except httpx.HTTPError:
        return None
    return contenido if contenido[:4] == b"%PDF" else None


def _hoy() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


# Una sola sincronización a la vez por proceso; las peticiones no esperan a la inicial
_SYNC_LOCK = asyncio.Lock()
_SIN_BOLETIN: set[datetime] = set()   # días pasados sin boletín (404): no se vuelven a pedir
_LEIDAS: set[datetime] = set()        # días ya leídos en este proceso (aunque faltara algún producto)
_INTENTOS: dict[datetime, float] = {}  # último intento sin boletín de los días recientes
_REINTENTO_S = 30 * 60                 # hoy/ayer se vuelven a consultar como mucho cada 30 min


async def sync_mercado(db: AsyncSession, days_back: int = 14) -> int:
    """Rellena los días recientes que falten. Descarga cada boletín una sola vez y guarda los 10
    productos de ese día. Devuelve cuántos precios nuevos se guardaron."""
    if _SYNC_LOCK.locked():
        return 0
    async with _SYNC_LOCK:
        hoy = _hoy()
        candidatos = [hoy - timedelta(days=i) for i in range(days_back)]
        existentes = await db.execute(
            select(database.PrecioMercado.producto, database.PrecioMercado.fecha).where(
                database.PrecioMercado.producto.in_(CLAVES),
                database.PrecioMercado.fecha.in_(candidatos),
            )
        )
        guardados: dict[datetime, set] = defaultdict(set)
        for producto, fecha in existentes.all():
            guardados[fecha].add(producto)
        ahora = time.monotonic()
        pendientes = [
            f for f in candidatos
            if len(guardados[f]) < len(CLAVES) and f not in _SIN_BOLETIN and f not in _LEIDAS
            and ahora - _INTENTOS.get(f, float("-inf")) >= _REINTENTO_S
        ]
        if not pendientes:
            return 0

        insertados = 0
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT, headers=_HTTP_HEADERS, follow_redirects=True) as client:
            for fecha in pendientes:
                pdf_bytes = await _fetch_pdf(client, fecha)
                if pdf_bytes is None:
                    if fecha < hoy - timedelta(days=2):
                        _SIN_BOLETIN.add(fecha)
                    else:                                  # aún puede publicarse: reintentar más tarde
                        _INTENTOS[fecha] = time.monotonic()
                    continue
                try:
                    filas = await asyncio.to_thread(_extract_rows, pdf_bytes)
                except Exception as exc:
                    print(f"[mercado] No se pudo leer el boletín del {fecha:%Y-%m-%d}: {exc}")
                    continue

                nuevos = []
                for producto in PRODUCTOS:
                    if producto["clave"] in guardados[fecha]:
                        continue
                    filas_producto = [f for f in filas if producto["regla"](f)]
                    if filas_producto:
                        nuevos.append({"producto": producto["clave"], "fuente": FUENTE_EMA, "fecha": fecha,
                                       **_aggregate(filas_producto)})
                if nuevos:
                    stmt = pg_insert(database.PrecioMercado).values(nuevos).on_conflict_do_nothing(
                        index_elements=["producto", "fecha"]   # restricción única (producto, fecha)
                    )
                    result = await db.execute(stmt)
                    await db.commit()
                    insertados += max(result.rowcount or 0, 0)
                _LEIDAS.add(fecha)
        return insertados


async def sync_choclo_prices(db: AsyncSession, days_back: int = 14) -> int:
    """Nombre anterior: ahora sincroniza los 10 productos."""
    return await sync_mercado(db, days_back)


_TAREA_INICIAL: Optional[asyncio.Task] = None


def iniciar_sincronizacion_inicial(dias: int = 120) -> None:
    """Rellena en segundo plano los últimos «dias» (sin retrasar el arranque de la API)."""
    global _TAREA_INICIAL

    async def _run() -> None:
        try:
            async with database.async_session() as session:
                n = await sync_mercado(session, days_back=dias)
            print(f"[mercado] Sincronización inicial: {n} precio(s) nuevo(s)")
        except Exception as exc:
            print(f"[mercado] Error en la sincronización inicial: {exc}")

    _TAREA_INICIAL = asyncio.get_running_loop().create_task(_run())


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


def _sin_atipicos(rows: list) -> list:
    """Quita precios que parecen errores de digitación del boletín (p. ej. un saco de zanahoria a
    $2 cuando los demás días cuesta entre $10 y $19): menos del 35 % o más de 2,85 veces la mediana
    del periodo. Con menos de 4 datos no se filtra nada."""
    if len(rows) < 4:
        return list(rows)
    mediana = statistics.median(float(r.precio) for r in rows)
    if mediana <= 0:
        return list(rows)
    return [r for r in rows if 0.35 * mediana <= float(r.precio) <= mediana / 0.35]


# «variedades» guarda cada variedad del día con su precio: «Papa Cecilia ($36), Papa Gabriela
# (Semichola) ($25), …» (formato de _aggregate). El nombre puede llevar paréntesis propios.
_VARIEDAD_RE = re.compile(r"(.+?) \(\$(\d+(?:\.\d+)?)\)(?:, |$)")


class _Punto(NamedTuple):
    fecha: datetime
    precio: float


def _parse_variedades(texto: Optional[str]) -> list[tuple[str, float]]:
    if not texto:
        return []
    return [(m.group(1).strip(), float(m.group(2))) for m in _VARIEDAD_RE.finditer(texto)]


def _resumen_serie(serie: list, corte: datetime) -> dict:
    """Último precio, comparación con el boletín de hace una semana y la serie desde «corte»."""
    actual = serie[-1]
    limite = actual.fecha - timedelta(days=7)
    anterior = next((r for r in reversed(serie) if r.fecha <= limite), None)
    tendencia, diff, pct = _tendencia(float(actual.precio), float(anterior.precio) if anterior else None)
    return {
        "precio": actual.precio,
        "fecha": actual.fecha,
        "precio_semana_anterior": anterior.precio if anterior else None,
        "fecha_semana_anterior": anterior.fecha if anterior else None,
        "variacion_semanal": diff,
        "variacion_semanal_pct": pct,
        "tendencia": tendencia,
    }


def _variedades_detalle(filas: list, corte: datetime) -> list[dict]:
    """Precio de cada variedad por separado (las del último boletín, en su orden), con su propia
    variación semanal y su serie. Vacío si el producto tiene una sola variedad."""
    if not filas:
        return []
    ultimas = [nombre for nombre, _ in _parse_variedades(filas[-1].variedades)]
    if len(ultimas) < 2:
        return []
    series: dict[str, list] = defaultdict(list)
    for fila in filas:
        for nombre, precio in _parse_variedades(fila.variedades):
            series[nombre].append(_Punto(fila.fecha, precio))
    detalle = []
    for nombre in ultimas:
        serie = _sin_atipicos(series[nombre])
        if not serie:
            continue
        detalle.append({
            "nombre": nombre,
            **_resumen_serie(serie, corte),
            "serie": [{"fecha": p.fecha, "precio": p.precio} for p in serie if p.fecha >= corte],
        })
    return detalle


async def _maiz_duro(db: AsyncSession) -> Optional[dict]:
    maiz_row = (await db.execute(
        select(database.PrecioMercado)
        .where(database.PrecioMercado.producto == "maiz_duro")
        .order_by(database.PrecioMercado.fecha.desc())
        .limit(1)
    )).scalars().first()
    if not maiz_row:
        return None
    return {
        "precio_promedio": maiz_row.precio,
        "precio_min": maiz_row.precio_min,
        "precio_max": maiz_row.precio_max,
        "unidad": maiz_row.presentacion,
        "fuente": maiz_row.fuente,
        "temporada": MAIZ_DURO_REFERENCIA["temporada"],
        "actualizado": maiz_row.fecha,
    }


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

    resultado["maiz_duro"] = await _maiz_duro(db)
    return resultado


async def get_productos(db: AsyncSession, dias: int = 90) -> dict:
    """Los 10 productos: último precio, variación frente a la semana anterior y su serie de «dias»."""
    dias = max(7, min(dias, 120))
    hoy = _hoy()
    corte = hoy - timedelta(days=dias)
    filas = (await db.execute(
        select(database.PrecioMercado)
        .where(
            database.PrecioMercado.producto.in_(CLAVES),
            database.PrecioMercado.fecha >= corte - timedelta(days=7),   # referencia de la semana anterior
        )
        .order_by(database.PrecioMercado.fecha.asc())
    )).scalars().all()
    por_producto = defaultdict(list)
    for fila in filas:
        por_producto[fila.producto].append(fila)

    productos = []
    for producto in PRODUCTOS:
        filas_producto = por_producto.get(producto["clave"], [])
        serie = _sin_atipicos(filas_producto)
        if not serie:
            continue
        actual = serie[-1]
        productos.append({
            "producto": producto["clave"],
            "nombre": producto["nombre"],
            "unidad": actual.presentacion,
            "precio_min": actual.precio_min,
            "precio_max": actual.precio_max,
            "variedades": actual.variedades,
            **_resumen_serie(serie, corte),
            "serie": [
                {"fecha": r.fecha, "precio": r.precio, "precio_min": r.precio_min, "precio_max": r.precio_max}
                for r in serie if r.fecha >= corte
            ],
            # Cada variedad por separado (papa, choclo, fréjol, cebolla, aguacate)
            "variedades_detalle": _variedades_detalle(filas_producto, corte),
        })

    return {
        "fuente": FUENTE_EMA,
        "fuente_url": AMBATO_LISTA_URL,
        "actualizado": max((p["fecha"] for p in productos), default=None),
        "productos": productos,
        "maiz_duro": await _maiz_duro(db),
    }


async def get_historico(db: AsyncSession, producto: str, dias: int) -> list[database.PrecioMercado]:
    desde = _hoy() - timedelta(days=dias)
    result = await db.execute(
        select(database.PrecioMercado)
        .where(database.PrecioMercado.producto == producto, database.PrecioMercado.fecha >= desde)
        .order_by(database.PrecioMercado.fecha.asc())
    )
    filas = result.scalars().all()
    return filas if producto == "maiz_duro" else _sin_atipicos(filas)
