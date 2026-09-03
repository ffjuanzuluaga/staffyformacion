# -*- coding: utf-8 -*-
"""Acceso a Odoo 19 (XML-RPC) y carga de datos del dashboard.

Clasificación de línea (en este orden, el primero que aplique):
  1. crm.team  → LINEA_TEAM
  2. sale.order.service_line / project.project.service_line (módulo l10n_co_firefly_project)
  3. staff_request_id (módulo firefly_staffing) → Staff

Así se rescatan ventas/facturas aunque Paula aún no haya asignado el equipo.
"""

from __future__ import annotations

import threading
import unicodedata
import xmlrpc.client

import pandas as pd
import streamlit as st

LINEA_TEAM = {
    "Staff": "Staffing IT",
    "Formación": "FORMACION",
    "Fábrica de Software": "FABRICA SOFTWARE",
}

# IDs de esta instancia (Staffing IT=11, FORMACION=6, FABRICA SOFTWARE=4).
# Sirven cuando crm.team no es visible para el usuario API pero las OV sí traen team_id.
KNOWN_TEAMS = {
    "Staff": {"id": 11, "name": "Staffing IT"},
    "Formación": {"id": 6, "name": "FORMACION"},
    "Fábrica de Software": {"id": 4, "name": "FABRICA SOFTWARE"},
}
TEAM_ID_HINTS = {v["id"]: k for k, v in KNOWN_TEAMS.items()}

LINEA_ANALYTIC = {
    "Staff": "Staffing IT",
    "Formación": "Formación TI",
    "Fábrica de Software": "Fábrica Software",
}
ANALYTIC_TO_LINEA = {v: k for k, v in LINEA_ANALYTIC.items()}
TEAM_TO_LINEA = {v: k for k, v in LINEA_TEAM.items()}

SERVICE_TO_LINEA = {
    "staff": "Staff",
    "training": "Formación",
    "software_factory": "Fábrica de Software",
}

# Match flexible de nombres de equipo (sin tildes/espacios/mayúsculas).
TEAM_ALIASES_NORM = {
    "staffingit": "Staff",
    "staffing": "Staff",
    "staff": "Staff",
    "formacion": "Formación",
    "formacionti": "Formación",
    "fabricasoftware": "Fábrica de Software",
    "fabrica": "Fábrica de Software",
    "fabricasw": "Fábrica de Software",
}

# Equipos CRM que no son las 3 líneas (no usar service_line como fallback).
# "formacion" es subcadena de "transformacion" — no mezclar esas OV/facturas.
OTHER_TEAMS_NORM = {
    "transformaciondigital",
    "transformacion",
}
# id=5 TRANSFORMACION DIGITAL en Firefly (no es Formación).
OTHER_TEAM_IDS = {5}
# Solo estos team_id entran al tablero (Fábrica=4, Formación=6, Staff=11).
ALLOWED_TEAM_IDS = set(TEAM_ID_HINTS.keys())
# Bust de caché Streamlit cuando cambia la lógica de clasificación.
_DATA_VERSION = 4


def norm_name(value) -> str:
    """Normaliza para comparar nombres de equipo/cuenta: minúsculas, sin tildes ni espacios."""
    s = str(value or "").strip().lower()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    return "".join(ch for ch in s if ch.isalnum())


def linea_from_team_name(nombre: str) -> str | None:
    if not nombre or nombre == "Sin asignar":
        return None
    # name_get a veces falla y deja False/None como "nombre"
    if nombre is False or nombre is None:
        return None
    nombre = str(nombre).strip()
    if not nombre or nombre.lower() in ("false", "none", "sin asignar"):
        return None
    exact = TEAM_TO_LINEA.get(nombre)
    if exact:
        return exact
    n = norm_name(nombre)
    for team_name, linea in TEAM_TO_LINEA.items():
        if norm_name(team_name) == n:
            return linea
    alias = TEAM_ALIASES_NORM.get(n)
    if alias:
        return alias
    if n in OTHER_TEAMS_NORM or n.startswith("transformacion"):
        return None
    # Último recurso: token propio, no subcadena (transformacion ≠ formacion)
    if "fabrica" in n and "software" in n:
        return "Fábrica de Software"
    if n.startswith("fabrica"):
        return "Fábrica de Software"
    if n.startswith("formacion"):
        return "Formación"
    if "staffing" in n or n == "staff":
        return "Staff"
    return None


def es_equipo_otra_linea(nombre) -> bool:
    n = norm_name(nombre)
    return bool(n) and (n in OTHER_TEAMS_NORM or n.startswith("transformacion"))

STAFF_ACTIVE_STATES = ("confirmed",)
STAFF_COVERAGE_STATES = ("confirmed", "done")
SUB_ACTIVE_STATES = ("3_progress", "4_paused")

# Tipos de actividad comercial que pidió Paula (match flexible, sin tildes).
TIPOS_COMERCIALES = (
    "presentacion",
    "propuesta",
    "socializacion",
    "seguimiento",
    "negocio",
)

_odoo_lock = threading.Lock()


# ─────────────────────────────────────────────
# Conexión
# ─────────────────────────────────────────────
@st.cache_resource(show_spinner="Conectando con Odoo...")
def get_connection():
    cfg = st.secrets["odoo"]
    common = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/common", allow_none=True)
    with _odoo_lock:
        uid = common.authenticate(cfg["db"], cfg["username"], cfg["api_key"], {})
    if not uid:
        st.error("❌ Autenticación fallida. Verifica url, db, username y api_key en los Secrets.")
        st.stop()
    models = xmlrpc.client.ServerProxy(f"{cfg['url']}/xmlrpc/2/object", allow_none=True)
    return cfg, uid, models


def odoo_call(model: str, method: str, args: list, kwargs: dict | None = None):
    cfg, uid, models = get_connection()
    with _odoo_lock:
        return models.execute_kw(cfg["db"], uid, cfg["api_key"], model, method, args, kwargs or {})


def search_read(model: str, domain: list, fields: list, context: dict | None = None, **kw) -> pd.DataFrame:
    kwargs = {"fields": fields, **kw}
    if context:
        kwargs["context"] = context
    records = odoo_call(model, "search_read", [domain], kwargs)
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=fields)
    return df


@st.cache_data(ttl=600, show_spinner=False)
def available_fields(model: str) -> set[str]:
    try:
        info = odoo_call(model, "fields_get", [], {"attributes": ["type"]})
        return set(info.keys())
    except Exception:
        return set()


def pick_fields(model: str, wanted: list[str]) -> list[str]:
    have = available_fields(model)
    if not have:
        return wanted
    return [f for f in wanted if f in have]


def m2o_name(series: pd.Series) -> pd.Series:
    def _name(v):
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            name = v[1]
            if name is False or name is None or str(name).strip() == "":
                return "Sin asignar"
            return str(name)
        return "Sin asignar"
    return series.apply(_name)


def m2o_id(series: pd.Series) -> pd.Series:
    def _id(v):
        if isinstance(v, (list, tuple)) and v:
            try:
                return int(v[0])
            except (TypeError, ValueError):
                return None
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        return None
    return series.apply(_id)


def m2o_set(series: pd.Series) -> pd.Series:
    """True si el many2one / many2many trae un valor real (no False/None)."""
    return series.apply(
        lambda v: bool(v) and v is not False and v != 0
        and not (isinstance(v, float) and pd.isna(v))
    )


def _team_id_linea_lookup(extra: dict | None = None) -> dict:
    mapping = dict(TEAM_ID_HINTS)
    try:
        mapping.update(team_id_to_linea_map())
    except Exception:
        pass
    if extra:
        mapping.update({int(k): v for k, v in extra.items()})
    # Nunca mapear TRANSFORMACION DIGITAL ni ids fuera de las 3 líneas
    return {
        int(k): v for k, v in mapping.items()
        if int(k) in ALLOWED_TEAM_IDS and int(k) not in OTHER_TEAM_IDS
    }


def _mask_equipo_excluido(df: pd.DataFrame) -> pd.Series:
    """True = equipo fuera del tablero (p.ej. TRANSFORMACION DIGITAL)."""
    excl = pd.Series(False, index=df.index)
    if "equipo" in df.columns:
        excl = excl | df["equipo"].apply(es_equipo_otra_linea)
    ids = None
    if "equipo_id" in df.columns:
        ids = pd.to_numeric(df["equipo_id"], errors="coerce")
    elif "team_id" in df.columns:
        ids = pd.to_numeric(m2o_id(df["team_id"]), errors="coerce")
    if ids is not None:
        has_team = ids.notna()
        excl = excl | ids.isin(list(OTHER_TEAM_IDS))
        # Cualquier otro team_id distinto de 4/6/11 queda fuera
        excl = excl | (has_team & ~ids.isin(list(ALLOWED_TEAM_IDS)))
    return excl.fillna(False)


def classify_linea(df: pd.DataFrame, team_id_to_linea: dict | None = None) -> pd.Series:
    out = pd.Series(pd.NA, index=df.index, dtype="object")
    other = _mask_equipo_excluido(df)
    if "equipo" in df.columns:
        out = df["equipo"].apply(linea_from_team_name)
    mapping = _team_id_linea_lookup(team_id_to_linea)
    if mapping and "team_id" in df.columns:
        ids = m2o_id(df["team_id"])
        by_id = ids.map(lambda i: mapping.get(int(i)) if pd.notna(i) and i is not None else None)
        # No rellenar filas de equipos excluidos
        out = out.where(out.notna() | other, by_id)
    if "service_line" in df.columns:
        fill = df["service_line"].map(SERVICE_TO_LINEA)
        out = out.where(out.notna() | other, fill)
    if "staff_request_id" in df.columns:
        has_staff = m2o_set(df["staff_request_id"])
        out = out.mask(out.isna() & ~other & has_staff, "Staff")
    out = out.mask(other, pd.NA)
    return out.fillna("Sin línea")


def gate_lineas_tablero(df: pd.DataFrame) -> pd.DataFrame:
    """Filtra al tablero y anula línea de equipos excluidos (defensa en profundidad)."""
    if df is None or df.empty or "linea" not in df.columns:
        return df if df is not None else pd.DataFrame()
    out = df.copy()
    excl = _mask_equipo_excluido(out)
    if excl.any():
        out.loc[excl, "linea"] = "Sin línea"
    return out[out["linea"].isin(list(LINEA_TEAM))].copy()


def month_list(start: str, n: int) -> list[str]:
    p = pd.Period(start, freq="M")
    return [str(p + i) for i in range(n)]


def coverage_mask(df: pd.DataFrame, start_col: str, end_col: str, mes: str) -> pd.Series:
    period = pd.Period(mes, freq="M")
    month_start = period.to_timestamp(how="start")
    month_end = period.to_timestamp(how="end")
    started = df[start_col].notna() & (df[start_col] <= month_end)
    not_ended = df[end_col].isna() | (df[end_col] >= month_start)
    return started & not_ended


# ─────────────────────────────────────────────
# Catálogos
# ─────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner="Cargando equipos de venta...")
def load_teams() -> pd.DataFrame:
    # active_test=False: incluir equipos archivados (si FABRICA está archivado,
    # sin esto no aparece y el tablero lo deja en cero).
    try:
        return search_read(
            "crm.team",
            [],
            ["id", "name", "active"],
            order="name",
            context={"active_test": False},
        )
    except Exception:
        return search_read("crm.team", [], ["id", "name"], order="name")


@st.cache_data(ttl=600, show_spinner=False)
def discover_teams_from_orders() -> dict:
    """Descubre equipos con read_group (todos los team_id usados en OV).

    Evita el límite de search_read(5000) que se quedaba en OV antiguas y
    nunca llegaba a FABRICA SOFTWARE (id=4 en la instancia Firefly).
    """
    resolved = {}
    groups = []
    try:
        groups = odoo_call(
            "sale.order",
            "read_group",
            [[("team_id", "!=", False)]],
            {"fields": ["team_id"], "groupby": ["team_id"], "lazy": False},
        )
    except Exception:
        groups = []

    for g in groups or []:
        raw = g.get("team_id")
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        tid, tname = int(raw[0]), str(raw[1] if raw[1] else "")
        linea = linea_from_team_name(tname)
        if not linea and tid:
            # name_get vacío: intentar leer crm.team por id
            try:
                tdf = search_read(
                    "crm.team", [("id", "=", tid)], ["id", "name"],
                    limit=1, context={"active_test": False},
                )
                if not tdf.empty:
                    tname = str(tdf.iloc[0]["name"])
                    linea = linea_from_team_name(tname)
            except Exception:
                pass
        if linea and linea not in resolved:
            resolved[linea] = {
                "id": tid,
                "name": tname or f"team_id={tid}",
                "active": True,
                "via": "sale.order.read_group",
            }

    # Pista conocida de la instancia (URL /odoo/sales-teams/4 = FABRICA SOFTWARE)
    if "Fábrica de Software" not in resolved:
        try:
            tdf = search_read(
                "crm.team",
                ["|", ("id", "=", 4), ("name", "ilike", "FABRICA")],
                ["id", "name", "active"],
                limit=20,
                context={"active_test": False},
            )
            for _, row in tdf.iterrows():
                linea = linea_from_team_name(row["name"]) or (
                    "Fábrica de Software" if int(row["id"]) == 4 else None
                )
                if linea == "Fábrica de Software":
                    resolved[linea] = {
                        "id": int(row["id"]),
                        "name": str(row["name"]),
                        "active": bool(row.get("active", True)),
                        "via": "crm.team.id/hint",
                    }
                    break
        except Exception:
            pass
    return resolved


@st.cache_data(ttl=600, show_spinner=False)
def resolve_linea_teams() -> dict:
    """Resuelve id/nombre de cada equipo. Primero crm.team; si falla,
    descubre desde read_group de OV (+ hint id=4 para Fábrica)."""
    resolved = {}
    ctx = {"active_test": False}

    # 1) Listar TODOS los equipos visibles y mapear por nombre normalizado
    try:
        all_teams = search_read(
            "crm.team", [], ["id", "name", "active"],
            order="name", context=ctx,
        )
    except Exception:
        all_teams = pd.DataFrame()
    if not all_teams.empty:
        for _, row in all_teams.iterrows():
            linea = linea_from_team_name(row["name"])
            if linea and linea not in resolved:
                resolved[linea] = {
                    "id": int(row["id"]),
                    "name": str(row["name"]),
                    "active": bool(row.get("active", True)),
                    "via": "crm.team",
                }

    # 2) Búsqueda explícita por cada nombre canónico
    for linea, nombre in LINEA_TEAM.items():
        if linea in resolved:
            continue
        try:
            df = search_read(
                "crm.team",
                ["|", ("name", "=ilike", nombre), ("name", "ilike", nombre.split()[0])],
                ["id", "name", "active"],
                limit=20,
                context=ctx,
            )
        except Exception:
            df = pd.DataFrame()
        if not df.empty:
            target = norm_name(nombre)
            best = None
            for _, row in df.iterrows():
                if norm_name(row["name"]) == target or linea_from_team_name(row["name"]) == linea:
                    best = row
                    break
            if best is None and linea == "Fábrica de Software":
                # Cualquier resultado con FABRICA en el nombre
                for _, row in df.iterrows():
                    if "fabrica" in norm_name(row["name"]):
                        best = row
                        break
            if best is not None:
                resolved[linea] = {
                    "id": int(best["id"]),
                    "name": str(best["name"]),
                    "active": bool(best.get("active", True)),
                    "via": "crm.team.search",
                }

    # 3) Completar desde OV (read_group) + hint id=4
    from_orders = discover_teams_from_orders()
    for linea, info in from_orders.items():
        if linea not in resolved:
            resolved[linea] = info

    # 4) IDs fijos de Firefly si el usuario API no puede leer crm.team
    for linea, hint in KNOWN_TEAMS.items():
        if linea not in resolved:
            resolved[linea] = {**hint, "active": True, "via": "id conocido"}
    return resolved


@st.cache_data(ttl=600, show_spinner=False)
def team_id_to_linea_map() -> dict:
    """Mapa {team_id:int → línea} para clasificar OV aunque name_get falle."""
    resolved = resolve_linea_teams()
    return {int(v["id"]): k for k, v in resolved.items()}


def team_id_for_linea(teams_df: pd.DataFrame, linea: str) -> int | None:
    resolved = resolve_linea_teams()
    if linea in resolved:
        return resolved[linea]["id"]
    if teams_df is None or teams_df.empty:
        return None
    target = norm_name(LINEA_TEAM.get(linea, ""))
    if not target:
        return None
    for _, row in teams_df.iterrows():
        n = norm_name(row["name"])
        if n == target or linea_from_team_name(row["name"]) == linea:
            return int(row["id"])
    return None


def all_team_ids(teams_df: pd.DataFrame) -> list[int]:
    return [tid for tid in (team_id_for_linea(teams_df, l) for l in LINEA_TEAM) if tid]


@st.cache_data(ttl=600, show_spinner=False)
def company_currency_id() -> int | None:
    """Moneda de la compañía actual (COP en Firefly)."""
    try:
        companies = search_read("res.company", [], ["currency_id"], limit=1)
        if companies.empty:
            return None
        return int(m2o_id(companies["currency_id"]).iloc[0] or 0) or None
    except Exception:
        return None


@st.cache_data(ttl=600, show_spinner=False)
def lookup_fx_rate(currency_id: int, date_str: str, company_id: int | None) -> float | None:
    """Busca TRM en res.currency.rate. Devuelve rate compañía→moneda (igual que sale.order.currency_rate)."""
    if not currency_id or not date_str:
        return None
    domain = [
        ("currency_id", "=", currency_id),
        ("name", "<=", date_str[:10]),
    ]
    if company_id:
        domain = domain + ["|", ("company_id", "=", False), ("company_id", "=", company_id)]
    try:
        rates = search_read(
            "res.currency.rate", domain,
            pick_fields("res.currency.rate", ["name", "rate", "company_rate", "inverse_company_rate"]),
            order="name desc", limit=1,
        )
    except Exception:
        return None
    if rates.empty:
        return None
    row = rates.iloc[0]
    # Preferir company_rate (moneda extranjera por 1 unidad de moneda compañía),
    # que es la misma convención que sale.order.currency_rate.
    for col in ("company_rate", "rate"):
        if col in row and pd.notna(row[col]) and float(row[col]) not in (0.0, 1.0):
            return float(row[col])
    if "inverse_company_rate" in row and pd.notna(row["inverse_company_rate"]):
        inv = float(row["inverse_company_rate"])
        if inv not in (0.0, 1.0):
            return 1.0 / inv
    return None


def amount_untaxed_company_series(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Convierte amount_untaxed a moneda compañía: amount / currency_rate.

    Si la OV está en USD y currency_rate es 1 (sin TRM), intenta res.currency.rate.
    Devuelve (montos_cop, alerta_por_fila) donde alerta=True = no se pudo convertir.
    """
    if df.empty or "amount_untaxed" not in df.columns:
        return pd.Series(dtype=float), pd.Series(dtype=bool)

    company_cur = company_currency_id()
    amounts = []
    alerts = []
    for _, row in df.iterrows():
        untaxed = float(row.get("amount_untaxed") or 0.0)
        cur_id = None
        if "currency_id" in df.columns:
            raw = row.get("currency_id")
            cur_id = raw[0] if isinstance(raw, (list, tuple)) else raw
            if cur_id is False:
                cur_id = None
        rate = float(row.get("currency_rate") or 0.0) if "currency_rate" in df.columns else 0.0
        same_currency = (not cur_id) or (company_cur and int(cur_id) == int(company_cur))

        if same_currency or untaxed == 0.0:
            amounts.append(untaxed)
            alerts.append(False)
            continue

        # rate≈1 con moneda extranjera ⇒ Odoo no tenía TRM al confirmar
        needs_lookup = (not rate) or abs(rate - 1.0) < 1e-12
        if needs_lookup:
            date_str = str(row.get("date_order") or "")[:10]
            company_id = None
            if "company_id" in df.columns:
                raw_c = row.get("company_id")
                company_id = raw_c[0] if isinstance(raw_c, (list, tuple)) else raw_c
                if company_id is False:
                    company_id = None
            fetched = lookup_fx_rate(int(cur_id), date_str, int(company_id) if company_id else None)
            if fetched:
                rate = fetched
                needs_lookup = False

        if rate and not needs_lookup:
            amounts.append(untaxed / rate)
            alerts.append(False)
        else:
            # Sin TRM: dejamos el número original y marcamos alerta (USD 1100 ≠ COP)
            amounts.append(untaxed)
            alerts.append(True)

    return pd.Series(amounts, index=df.index), pd.Series(alerts, index=df.index)


@st.cache_data(ttl=600, show_spinner="Cargando metas por línea...")
def load_metas_lineas() -> pd.DataFrame:
    try:
        df = pd.read_csv("data/metas_lineas.csv")
        df["linea"] = df["linea"].str.strip()
        return df
    except FileNotFoundError:
        return pd.DataFrame(columns=["linea", "meta_anual"])


@st.cache_data(ttl=600, show_spinner="Cargando costos fijos por línea...")
def load_costos_fijos() -> pd.DataFrame:
    try:
        df = pd.read_csv("data/costos_fijos.csv")
        df["linea"] = df["linea"].str.strip()
        return df
    except FileNotFoundError:
        return pd.DataFrame(columns=["linea", "persona", "costo_mensual"])


# ─────────────────────────────────────────────
# CRM / ventas / facturas
# ─────────────────────────────────────────────
def _linea_or_domain(team_ids: list[int], fields: set[str]) -> list:
    """OR: equipo de venta O service_line de las 3 líneas O staff_request."""
    clauses: list = []
    n_or = 0
    if team_ids:
        clauses += [("team_id", "in", team_ids)]
        n_or += 1
    if "service_line" in fields:
        clauses += [("service_line", "in", list(SERVICE_TO_LINEA))]
        n_or += 1
    if "staff_request_id" in fields:
        clauses += [("staff_request_id", "!=", False)]
        n_or += 1
    if not clauses:
        return []
    return ["|"] * (n_or - 1) + clauses


@st.cache_data(ttl=600, show_spinner="Cargando leads y pipeline...")
def load_leads_full(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    domain = [
        ("create_date", ">=", date_from),
        ("create_date", "<=", f"{date_to} 23:59:59"),
        "|", ("active", "=", True), ("active", "=", False),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    fields = pick_fields(
        "crm.lead",
        ["name", "create_date", "date_closed", "date_deadline", "user_id", "team_id",
         "source_id", "expected_revenue", "probability", "active", "type", "won_status"],
    )
    df = search_read("crm.lead", domain, fields, order="create_date")
    if df.empty:
        return df
    df["create_date"] = pd.to_datetime(df["create_date"])
    df["mes"] = df["create_date"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"]) if "user_id" in df else "Sin asignar"
    df["equipo"] = m2o_name(df["team_id"]) if "team_id" in df else "Sin asignar"
    df["origen"] = m2o_name(df["source_id"]) if "source_id" in df else "Sin asignar"
    df["linea"] = classify_linea(df)
    df["estado"] = df["won_status"].map({"won": "Ganada", "lost": "Perdida", "pending": "Abierta"})
    return df


@st.cache_data(ttl=600, show_spinner="Cargando oportunidades ganadas...")
def load_won(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    domain = [
        ("type", "=", "opportunity"),
        ("won_status", "=", "won"),
        ("date_closed", ">=", date_from),
        ("date_closed", "<=", f"{date_to} 23:59:59"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    fields = pick_fields(
        "crm.lead",
        ["name", "date_closed", "user_id", "team_id", "partner_id", "expected_revenue", "source_id"],
    )
    df = search_read("crm.lead", domain, fields, order="date_closed")
    if df.empty:
        return df
    df["date_closed"] = pd.to_datetime(df["date_closed"])
    df["mes"] = df["date_closed"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    df["origen"] = m2o_name(df["source_id"]) if "source_id" in df else "Sin asignar"
    df["linea"] = classify_linea(df)
    return df


@st.cache_data(ttl=600, show_spinner="Cargando pipeline abierto (proyección)...")
def load_open_pipeline(team_ids: list[int]) -> pd.DataFrame:
    """Oportunidades abiertas con fecha de cierre esperada — proyección real, no promedio."""
    domain = [
        ("type", "=", "opportunity"),
        ("won_status", "=", "pending"),
        ("active", "=", True),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    fields = pick_fields(
        "crm.lead",
        ["name", "date_deadline", "user_id", "team_id", "partner_id",
         "expected_revenue", "probability", "source_id"],
    )
    df = search_read("crm.lead", domain, fields, order="date_deadline")
    if df.empty:
        return df
    df["date_deadline"] = pd.to_datetime(df["date_deadline"], errors="coerce")
    df["mes"] = df["date_deadline"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    df["linea"] = classify_linea(df)
    return df


@st.cache_data(ttl=600, show_spinner="Cargando órdenes de venta...")
def load_sales(date_from: str, date_to: str, team_ids: list[int],
               _v: int = _DATA_VERSION) -> pd.DataFrame:
    """OV confirmadas. Montos s/imp. en moneda compañía (COP).

    IMPORTANTE: NO filtramos por team_id / team_id.name en el dominio.
    Buscar por team_id.name aplica reglas de crm.team y Oculta FABRICA SOFTWARE
    si el usuario API no puede leer ese equipo. En cambio leemos TODAS las OV
    confirmadas del período y clasificamos en cliente con el nombre del many2one.
    Solo pasan al tablero team_id ∈ {4,6,11} (o sin equipo + service_line).
    """
    del team_ids, _v
    domain = [
        ("date_order", ">=", date_from),
        ("date_order", "<=", f"{date_to} 23:59:59"),
        ("state", "in", ["sale", "done"]),
    ]
    fields = pick_fields(
        "sale.order",
        ["name", "date_order", "partner_id", "user_id", "team_id",
         "amount_untaxed", "amount_total", "currency_id", "currency_rate",
         "company_id", "service_line", "staff_request_id",
         "invoice_ids", "opportunity_id"],
    )
    try:
        df = search_read("sale.order", domain, fields, order="date_order")
    except Exception:
        fields = ["name", "date_order", "partner_id", "user_id", "team_id",
                  "amount_untaxed", "amount_total", "currency_id", "currency_rate",
                  "invoice_ids"]
        df = search_read("sale.order", domain, fields, order="date_order")

    if df.empty:
        return df

    df["date_order"] = pd.to_datetime(df["date_order"])
    df["mes"] = df["date_order"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"]) if "user_id" in df else "Sin asignar"
    df["equipo"] = m2o_name(df["team_id"]) if "team_id" in df else "Sin asignar"
    df["equipo_id"] = m2o_id(df["team_id"]) if "team_id" in df else None
    df["cliente"] = m2o_name(df["partner_id"]) if "partner_id" in df else "Sin asignar"
    df["moneda"] = m2o_name(df["currency_id"]) if "currency_id" in df else "COP"
    df["linea"] = classify_linea(df)

    untaxed = pd.to_numeric(df.get("amount_untaxed", 0), errors="coerce").fillna(0.0)
    rate = pd.to_numeric(df["currency_rate"], errors="coerce") if "currency_rate" in df else pd.Series(1.0, index=df.index)
    rate = rate.replace(0, pd.NA).fillna(1.0)
    company_cur = company_currency_id()
    cur_ids = m2o_id(df["currency_id"]) if "currency_id" in df else pd.Series([None] * len(df), index=df.index)
    same_cur = cur_ids.isna() | (cur_ids.astype("Int64") == company_cur) if company_cur else cur_ids.isna()
    foreign_without_rate = (~same_cur) & (rate.sub(1.0).abs() < 1e-12)
    df["amount_untaxed_company"] = untaxed.where(same_cur | foreign_without_rate, untaxed / rate)
    df.loc[foreign_without_rate, "amount_untaxed_company"] = untaxed.loc[foreign_without_rate]
    df["fx_sin_trm"] = foreign_without_rate.fillna(False)

    excl = _mask_equipo_excluido(df)
    meta = {}
    if excl.any():
        scol = "amount_untaxed_company" if "amount_untaxed_company" in df.columns else "amount_untaxed"
        meta = {
            "excluidas_ov": int(excl.sum()),
            "excluidas_monto": float(pd.to_numeric(df.loc[excl, scol], errors="coerce").fillna(0).sum()),
            "excluidas_equipos": (
                df.loc[excl].groupby(df.loc[excl, "equipo"].astype(str))[scol]
                .sum()
                .round(0)
                .astype(int)
                .to_dict()
                if "equipo" in df.columns else {}
            ),
        }
    out = gate_lineas_tablero(df)
    out.attrs.update(meta)
    return out


@st.cache_data(ttl=600, show_spinner="Cargando facturas...")
def load_invoices(date_from: str, date_to: str, team_ids: list[int],
                  extra_ids: list[int] | None = None,
                  _v: int = _DATA_VERSION) -> pd.DataFrame:
    """Facturas posted del período. Sin filtro team_id.name (mismas reglas CRM).

    Clasifica en cliente. Si vienen extra_ids de OV de las 3 líneas, se unen.
    Solo team_id ∈ {4,6,11} (u OV de esas líneas sin equipo en la factura).
    """
    del _v
    domain = [
        ("move_type", "in", ["out_invoice", "out_refund"]),
        ("state", "=", "posted"),
        ("invoice_date", ">=", date_from),
        ("invoice_date", "<=", date_to),
    ]
    # Solo ids de las 3 líneas — nunca TRANSFORMACION DIGITAL (5)
    resolved = resolve_linea_teams()
    ids = list({
        tid for tid in (*(team_ids or []), *(v["id"] for v in resolved.values()))
        if tid in ALLOWED_TEAM_IDS and tid not in OTHER_TEAM_IDS
    })
    ors: list = []
    if ids:
        ors.append(("team_id", "in", ids))
    if extra_ids:
        ors.append(("id", "in", extra_ids))
    if ors:
        domain = domain + (["|"] * (len(ors) - 1) + ors)

    fields = pick_fields(
        "account.move",
        ["name", "invoice_date", "partner_id", "invoice_user_id", "team_id",
         "amount_total_signed", "amount_untaxed_signed", "payment_state", "move_type"],
    )
    df = search_read("account.move", domain, fields, order="invoice_date")
    if df.empty:
        return df
    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    df["mes"] = df["invoice_date"].dt.to_period("M").astype(str)
    df["equipo"] = m2o_name(df["team_id"]) if "team_id" in df else "Sin asignar"
    df["equipo_id"] = m2o_id(df["team_id"]) if "team_id" in df else None
    df["vendedor"] = m2o_name(df["invoice_user_id"]) if "invoice_user_id" in df else "Sin asignar"
    df["cliente"] = m2o_name(df["partner_id"])
    df["linea"] = classify_linea(df)
    return gate_lineas_tablero(df)


def invoice_ids_from_sales(sales: pd.DataFrame) -> list[int]:
    if sales.empty or "invoice_ids" not in sales.columns:
        return []
    ids: set[int] = set()
    for raw in sales["invoice_ids"]:
        if isinstance(raw, list):
            ids.update(int(i) for i in raw)
    return sorted(ids)


# ─────────────────────────────────────────────
# Staff: solicitudes + suscripciones + renovaciones
# ─────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner="Cargando solicitudes de Staff (firefly_staffing)...")
def load_staffing_requests():
    cols = [
        "name", "partner_id", "role_id", "resource_partner_id", "state",
        "date_start", "date_end", "monthly_amount_company_currency",
        "purchase_amount_company_currency", "margin_company_currency",
        "user_id", "crm_lead_id", "current_subscription_id",
    ]
    if "firefly.staffing.request" not in _models_exist(("firefly.staffing.request",)):
        return pd.DataFrame(columns=cols), (
            "El módulo `firefly_staffing` no está disponible en esta base. "
            "Las plazas se calculan con suscripciones (sale.order)."
        )
    fields = pick_fields("firefly.staffing.request", cols)
    try:
        df = search_read(
            "firefly.staffing.request",
            [("state", "in", ["confirmed", "done", "profile"])],
            fields,
            order="date_start desc",
        )
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo leer firefly.staffing.request: {e}"
    if df.empty:
        return df, None
    df["cliente"] = m2o_name(df["partner_id"])
    df["rol"] = m2o_name(df["role_id"]) if "role_id" in df else "Sin rol"
    df["recurso"] = m2o_name(df["resource_partner_id"]) if "resource_partner_id" in df else "Sin recurso"
    df["responsable"] = m2o_name(df["user_id"]) if "user_id" in df else "Sin asignar"
    df["date_start"] = pd.to_datetime(df["date_start"], errors="coerce")
    df["date_end"] = pd.to_datetime(df["date_end"], errors="coerce")
    for col in ("monthly_amount_company_currency", "purchase_amount_company_currency",
                "margin_company_currency"):
        if col not in df:
            df[col] = 0.0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando historial de renovaciones Staff...")
def load_staffing_renewals(date_from: str, date_to: str):
    cols = ["date", "event_type", "staff_request_id", "subscription_id", "notes", "user_id"]
    if "firefly.staffing.history" not in _models_exist(("firefly.staffing.history",)):
        return pd.DataFrame(columns=cols), None
    try:
        df = search_read(
            "firefly.staffing.history",
            [
                ("event_type", "=", "renewal"),
                ("date", ">=", date_from),
                ("date", "<=", f"{date_to} 23:59:59"),
            ],
            pick_fields("firefly.staffing.history", cols),
            order="date",
        )
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo leer firefly.staffing.history: {e}"
    if df.empty:
        return df, None
    df["date"] = pd.to_datetime(df["date"])
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["staff"] = m2o_name(df["staff_request_id"]) if "staff_request_id" in df else ""
    df["usuario"] = m2o_name(df["user_id"]) if "user_id" in df else ""
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando suscripciones de Staff...")
def load_subscriptions(team_id: int | None):
    wanted = [
        "name", "partner_id", "subscription_state", "start_date", "end_date",
        "team_id", "next_invoice_date", "recurring_monthly", "staff_request_id",
        "user_id", "amount_untaxed",
    ]
    have = available_fields("sale.order")
    domain = []
    if "is_subscription" in have:
        domain.append(("is_subscription", "=", True))
    elif "plan_id" in have:
        domain.append(("plan_id", "!=", False))
    else:
        return pd.DataFrame(columns=wanted), (
            "Esta base no expone is_subscription ni plan_id en sale.order. "
            "Revisa que sale_subscription esté instalado."
        )
    if team_id:
        domain.append(("team_id", "=", team_id))
    # (is_subscription AND team) OR staff_request — rescata plazas sin equipo asignado.
    if "staff_request_id" in have:
        domain = ["|", ("staff_request_id", "!=", False)] + (
            ["&"] + domain if len(domain) > 1 else domain
        )

    try:
        df = search_read("sale.order", domain, pick_fields("sale.order", wanted))
    except Exception as e:
        return pd.DataFrame(columns=wanted), f"No se pudo consultar suscripciones: {e}"
    if df.empty:
        return df, None
    df["cliente"] = m2o_name(df["partner_id"])
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df["end_date"] = pd.to_datetime(df["end_date"], errors="coerce")
    if "next_invoice_date" in df:
        df["next_invoice_date"] = pd.to_datetime(df["next_invoice_date"], errors="coerce")
    df["equipo"] = m2o_name(df["team_id"]) if "team_id" in df else "Sin asignar"
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando log de suscripciones (MRR / renovaciones)...")
def load_subscription_logs(date_from: str, date_to: str, team_ids: list[int]):
    cols = ["event_type", "event_date", "order_id", "team_id", "amount_signed",
            "recurring_monthly", "subscription_state"]
    if "sale.order.log" not in _models_exist(("sale.order.log",)):
        return pd.DataFrame(columns=cols), None
    domain = [
        ("event_date", ">=", date_from),
        ("event_date", "<=", date_to),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    try:
        df = search_read("sale.order.log", domain, pick_fields("sale.order.log", cols))
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo leer sale.order.log: {e}"
    if df.empty:
        return df, None
    df["event_date"] = pd.to_datetime(df["event_date"], errors="coerce")
    df["mes"] = df["event_date"].dt.to_period("M").astype(str)
    df["suscripcion"] = m2o_name(df["order_id"]) if "order_id" in df else ""
    df["equipo"] = m2o_name(df["team_id"]) if "team_id" in df else "Sin asignar"
    return df, None


def staffing_coverage(df: pd.DataFrame, months: list[str],
                      states: tuple[str, ...] = STAFF_COVERAGE_STATES) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame({"mes": months, "activas": [0] * len(months)})
    sub = df[df["state"].isin(states)] if "state" in df.columns else df
    rows = []
    for mes in months:
        rows.append({"mes": mes, "activas": int(coverage_mask(sub, "date_start", "date_end", mes).sum())})
    return pd.DataFrame(rows)


def subscription_coverage(df: pd.DataFrame, months: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame({"mes": months, "activas": [0] * len(months)})
    sub = df
    if "subscription_state" in df.columns:
        sub = df[df["subscription_state"].isin(SUB_ACTIVE_STATES)]
    rows = []
    for mes in months:
        rows.append({"mes": mes, "activas": int(coverage_mask(sub, "start_date", "end_date", mes).sum())})
    return pd.DataFrame(rows)


def staffing_pnl_monthly(requests: pd.DataFrame, months: list[str],
                         costo_fijo_mensual: float) -> pd.DataFrame:
    """Ingreso de plazas − costo del recurso − costo fijo (Diego) por mes."""
    rows = []
    usable = requests[requests["state"].isin(STAFF_COVERAGE_STATES)] if not requests.empty else requests
    for mes in months:
        if usable.empty:
            rows.append({
                "mes": mes, "plazas": 0, "ingreso_plazas": 0.0,
                "costo_recurso": 0.0, "costo_fijo": costo_fijo_mensual,
                "neto": -costo_fijo_mensual, "valor_recurso_promedio": 0.0,
            })
            continue
        mask = coverage_mask(usable, "date_start", "date_end", mes)
        active = usable[mask]
        ingreso = float(active["monthly_amount_company_currency"].sum())
        costo = float(active["purchase_amount_company_currency"].sum())
        n = int(len(active))
        rows.append({
            "mes": mes,
            "plazas": n,
            "ingreso_plazas": ingreso,
            "costo_recurso": costo,
            "costo_fijo": costo_fijo_mensual,
            "neto": ingreso - costo - costo_fijo_mensual,
            "valor_recurso_promedio": (costo / n) if n else 0.0,
        })
    return pd.DataFrame(rows)


# ─────────────────────────────────────────────
# Proyectos (Formación / Fábrica) — l10n_co_firefly_project
# ─────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner="Cargando proyectos por línea de servicio...")
def load_projects(date_from: str, date_to: str):
    cols = [
        "name", "date_start", "date", "create_date", "partner_id", "user_id",
        "service_line", "sale_order_id", "metodologia_progress",
    ]
    have = available_fields("project.project")
    if "service_line" not in have:
        return pd.DataFrame(columns=cols), (
            "project.project no tiene `service_line` (módulo l10n_co_firefly_project). "
            "Cursos/proyectos entregados se estiman con oportunidades ganadas."
        )
    domain = [
        ("service_line", "in", ["training", "software_factory"]),
        "|", ("active", "=", True), ("active", "=", False),
    ]
    try:
        df = search_read("project.project", domain, pick_fields("project.project", cols))
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo leer project.project: {e}"
    if df.empty:
        return df, None
    df["date_start"] = pd.to_datetime(df["date_start"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df["create_date"] = pd.to_datetime(df["create_date"], errors="coerce")
    # Fecha de entrega proxy: date (fin de proyecto). Si no hay, date_start, luego create_date.
    # El campo dedicado de entrega de capacitaciones aún no existe (pendiente JUAN Z).
    entrega = df["date"].where(df["date"].notna(), df["date_start"])
    df["fecha_entrega"] = entrega.where(entrega.notna(), df["create_date"])
    df["mes"] = df["fecha_entrega"].dt.to_period("M").astype(str)
    df["cliente"] = m2o_name(df["partner_id"]) if "partner_id" in df else "Sin asignar"
    df["responsable"] = m2o_name(df["user_id"]) if "user_id" in df else "Sin asignar"
    df["linea"] = df["service_line"].map(SERVICE_TO_LINEA).fillna("Sin línea")
    lo = pd.Timestamp(date_from)
    hi = pd.Timestamp(date_to) + pd.Timedelta(days=1)
    df = df[df["fecha_entrega"].between(lo, hi, inclusive="left")]
    return df, None


# ─────────────────────────────────────────────
# Analítica: costos y horas
# ─────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner="Cargando costos analíticos por línea...")
def load_analytic_costs(date_from: str, date_to: str):
    cols = ["date", "amount", "account_id"]
    domain = [
        ("account_id.name", "in", list(LINEA_ANALYTIC.values())),
        ("date", ">=", date_from), ("date", "<=", date_to),
        ("amount", "<", 0),
    ]
    try:
        df = search_read("account.analytic.line", domain, cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), (
            f"No se pudo consultar costos analíticos: {e}. "
            f"Cuentas esperadas: {', '.join(LINEA_ANALYTIC.values())}."
        )
    if df.empty:
        return df, None
    df["date"] = pd.to_datetime(df["date"])
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["linea"] = m2o_name(df["account_id"]).map(ANALYTIC_TO_LINEA).fillna("Sin línea")
    df["costo"] = -df["amount"]
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando horas del equipo por línea...")
def load_analytic_hours(date_from: str, date_to: str):
    cols = ["date", "unit_amount", "account_id", "employee_id", "name"]
    domain = [
        ("account_id.name", "in", list(LINEA_ANALYTIC.values())),
        ("date", ">=", date_from), ("date", "<=", date_to),
        ("unit_amount", ">", 0),
    ]
    try:
        df = search_read("account.analytic.line", domain, pick_fields("account.analytic.line", cols))
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo consultar horas del equipo: {e}"
    if df.empty:
        return df, None
    df["date"] = pd.to_datetime(df["date"])
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["linea"] = m2o_name(df["account_id"]).map(ANALYTIC_TO_LINEA).fillna("Sin línea")
    df["persona"] = m2o_name(df["employee_id"]) if "employee_id" in df else "Sin asignar"
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando vendedores de los equipos de venta...")
def load_sales_team_employees(team_ids: list[int]) -> pd.DataFrame:
    cols = ["employee_id", "user_id", "vendedor"]
    if not team_ids:
        return pd.DataFrame(columns=cols)
    teams = search_read("crm.team", [("id", "in", team_ids)], ["id", "member_ids"])
    if teams.empty:
        return pd.DataFrame(columns=cols)
    user_ids = sorted({uid for ids in teams["member_ids"] for uid in (ids or [])})
    if not user_ids:
        return pd.DataFrame(columns=cols)
    emp = search_read("hr.employee", [("user_id", "in", user_ids)], ["id", "user_id", "name"])
    if emp.empty:
        return pd.DataFrame(columns=cols)
    emp["user_id"] = m2o_id(emp["user_id"])
    return emp.rename(columns={"id": "employee_id", "name": "vendedor"})[cols]


@st.cache_data(ttl=600, show_spinner="Cargando horas por proyecto...")
def load_hours_by_project(date_from: str, date_to: str, employee_ids: list[int]):
    cols = ["date", "unit_amount", "project_id", "employee_id"]
    domain = [
        ("date", ">=", date_from), ("date", "<=", date_to),
        ("unit_amount", ">", 0),
        ("project_id", "!=", False),
    ]
    if employee_ids:
        domain.append(("employee_id", "in", employee_ids))
    try:
        df = search_read("account.analytic.line", domain, cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo consultar horas por proyecto: {e}"
    if df.empty:
        return df, None
    df["date"] = pd.to_datetime(df["date"])
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["proyecto"] = m2o_name(df["project_id"])
    df["vendedor"] = m2o_name(df["employee_id"])
    return df, None


# ─────────────────────────────────────────────
# Actividades: histórico (crm.activity.report) + backlog + horas (calendar)
# ─────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner="Cargando actividades históricas del CRM...")
def load_activity_report(date_from: str, date_to: str, team_ids: list[int]):
    """Actividades YA hechas: crm.activity.report lee mail.message con
    mail_activity_type_id (Odoo no guarda mail.activity al completarlas)."""
    cols = ["date", "mail_activity_type_id", "user_id", "team_id", "author_id",
            "lead_id", "won_status"]
    if "crm.activity.report" not in _models_exist(("crm.activity.report",)):
        return pd.DataFrame(columns=cols), (
            "crm.activity.report no está disponible. Se usará solo el backlog de mail.activity."
        )
    domain = [
        ("date", ">=", date_from),
        ("date", "<=", f"{date_to} 23:59:59"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    try:
        df = search_read("crm.activity.report", domain, pick_fields("crm.activity.report", cols))
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo leer crm.activity.report: {e}"
    if df.empty:
        return df, None
    df["date"] = pd.to_datetime(df["date"])
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["tipo"] = m2o_name(df["mail_activity_type_id"]) if "mail_activity_type_id" in df else "Sin tipo"
    df["vendedor"] = m2o_name(df["user_id"]) if "user_id" in df else "Sin asignar"
    df["equipo"] = m2o_name(df["team_id"]) if "team_id" in df else "Sin asignar"
    df["linea"] = classify_linea(df)
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando actividades pendientes...")
def load_team_activities(team_ids: list[int]):
    cols = ["res_id", "activity_type_id", "user_id", "date_deadline", "create_date"]
    try:
        df = search_read("mail.activity", [("res_model", "=", "crm.lead")], cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo consultar mail.activity: {e}"
    if df.empty:
        return df, None
    df["tipo"] = m2o_name(df["activity_type_id"])
    df["vendedor"] = m2o_name(df["user_id"])
    df["date_deadline"] = pd.to_datetime(df["date_deadline"])
    df["mes"] = df["date_deadline"].dt.to_period("M").astype(str)
    res_ids = sorted({int(rid) for rid in df["res_id"].dropna().unique()})
    leads_domain = [("id", "in", res_ids)]
    if team_ids:
        leads_domain.append(("team_id", "in", team_ids))
    leads_teams = (
        search_read("crm.lead", leads_domain, ["id", "team_id"])
        if res_ids else pd.DataFrame(columns=["id", "team_id"])
    )
    if leads_teams.empty:
        return df.iloc[0:0], None
    leads_teams["equipo"] = m2o_name(leads_teams["team_id"])
    df = df.merge(leads_teams[["id", "equipo"]], left_on="res_id", right_on="id", how="inner")
    df["linea"] = classify_linea(df)
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando horas de reuniones CRM (calendario)...")
def load_calendar_hours(date_from: str, date_to: str, team_ids: list[int]):
    """Horas de calendar.event ligadas a una oportunidad — proxy de
    'horas dedicadas a cada tipo de actividad' (mail.activity no guarda duración)."""
    cols = ["name", "start", "duration", "opportunity_id", "user_id"]
    have = available_fields("calendar.event")
    if "opportunity_id" not in have:
        return pd.DataFrame(columns=cols + ["linea", "persona", "mes"]), (
            "calendar.event no tiene opportunity_id. No se pueden cruzar reuniones con el CRM."
        )
    domain = [
        ("start", ">=", date_from),
        ("start", "<=", f"{date_to} 23:59:59"),
        ("opportunity_id", "!=", False),
    ]
    try:
        df = search_read("calendar.event", domain, pick_fields("calendar.event", cols))
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo leer calendar.event: {e}"
    if df.empty:
        return df, None
    df["start"] = pd.to_datetime(df["start"])
    df["mes"] = df["start"].dt.to_period("M").astype(str)
    df["persona"] = m2o_name(df["user_id"]) if "user_id" in df else "Sin asignar"
    df["duration"] = pd.to_numeric(df.get("duration", 0), errors="coerce").fillna(0.0)
    opp_ids = [int(i) for i in m2o_id(df["opportunity_id"]).dropna().unique()]
    leads_domain = [("id", "in", opp_ids)]
    if team_ids:
        leads_domain.append(("team_id", "in", team_ids))
    leads = (
        search_read("crm.lead", leads_domain, ["id", "team_id", "name"])
        if opp_ids else pd.DataFrame(columns=["id", "team_id"])
    )
    if leads.empty:
        return df.iloc[0:0], None
    leads["equipo"] = m2o_name(leads["team_id"])
    df["opp_id"] = m2o_id(df["opportunity_id"])
    df = df.merge(leads[["id", "equipo"]], left_on="opp_id", right_on="id", how="inner")
    df["linea"] = classify_linea(df)
    df["tipo"] = df["name"].fillna("Reunión").astype(str)
    return df, None


def es_tipo_comercial(nombre: str) -> bool:
    n = (
        str(nombre).lower()
        .replace("á", "a").replace("é", "e").replace("í", "i")
        .replace("ó", "o").replace("ú", "u")
    )
    return any(t in n for t in TIPOS_COMERCIALES)


# ─────────────────────────────────────────────
# Helpers internos
# ─────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner=False)
def _models_exist(names: tuple[str, ...]) -> set[str]:
    found = set()
    for name in names:
        try:
            odoo_call(name, "fields_get", [], {"attributes": ["type"]})
            found.add(name)
        except Exception:
            continue
    return found
