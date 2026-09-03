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
import xmlrpc.client

import pandas as pd
import streamlit as st

LINEA_TEAM = {
    "Staff": "Staffing IT",
    "Formación": "FORMACION",
    "Fábrica de Software": "FABRICA SOFTWARE",
}

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


def search_read(model: str, domain: list, fields: list, **kw) -> pd.DataFrame:
    records = odoo_call(model, "search_read", [domain], {"fields": fields, **kw})
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
    return series.apply(
        lambda v: v[1] if isinstance(v, (list, tuple)) and len(v) == 2 else "Sin asignar"
    )


def m2o_id(series: pd.Series) -> pd.Series:
    return series.apply(lambda v: v[0] if isinstance(v, (list, tuple)) else None)


def m2o_set(series: pd.Series) -> pd.Series:
    """True si el many2one / many2many trae un valor real (no False/None)."""
    return series.apply(
        lambda v: bool(v) and v is not False and v != 0
        and not (isinstance(v, float) and pd.isna(v))
    )


def classify_linea(df: pd.DataFrame) -> pd.Series:
    out = pd.Series(pd.NA, index=df.index, dtype="object")
    if "equipo" in df.columns:
        out = df["equipo"].map(TEAM_TO_LINEA)
    if "service_line" in df.columns:
        fill = df["service_line"].map(SERVICE_TO_LINEA)
        out = out.where(out.notna(), fill)
    if "staff_request_id" in df.columns:
        has_staff = m2o_set(df["staff_request_id"])
        out = out.mask(out.isna() & has_staff, "Staff")
    return out.fillna("Sin línea")


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
    return search_read("crm.team", [], ["id", "name"], order="name")


def team_id_for_linea(teams_df: pd.DataFrame, linea: str) -> int | None:
    nombre = LINEA_TEAM.get(linea)
    match = teams_df.loc[teams_df["name"] == nombre, "id"]
    return int(match.iloc[0]) if not match.empty else None


def all_team_ids(teams_df: pd.DataFrame) -> list[int]:
    return [tid for tid in (team_id_for_linea(teams_df, l) for l in LINEA_TEAM) if tid]


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
def load_sales(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    have = available_fields("sale.order")
    domain = [
        ("date_order", ">=", date_from),
        ("date_order", "<=", f"{date_to} 23:59:59"),
        ("state", "in", ["sale", "done"]),
    ] + _linea_or_domain(team_ids, have)
    fields = pick_fields(
        "sale.order",
        ["name", "date_order", "partner_id", "user_id", "team_id",
         "amount_untaxed", "amount_total", "service_line", "staff_request_id",
         "invoice_ids", "opportunity_id"],
    )
    df = search_read("sale.order", domain, fields, order="date_order")
    if df.empty:
        return df
    df["date_order"] = pd.to_datetime(df["date_order"])
    df["mes"] = df["date_order"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    df["linea"] = classify_linea(df)
    return df


@st.cache_data(ttl=600, show_spinner="Cargando facturas...")
def load_invoices(date_from: str, date_to: str, team_ids: list[int],
                  extra_ids: list[int] | None = None) -> pd.DataFrame:
    """Facturas posted. Une las del equipo + las ligadas a OV de la línea
    (por si el team_id de la factura aún no está asignado)."""
    domain = [
        ("move_type", "in", ["out_invoice", "out_refund"]),
        ("state", "=", "posted"),
        ("invoice_date", ">=", date_from),
        ("invoice_date", "<=", date_to),
    ]
    team_clause = [("team_id", "in", team_ids)] if team_ids else []
    extra_clause = [("id", "in", extra_ids)] if extra_ids else []
    if team_clause and extra_clause:
        domain = domain + ["|"] + team_clause + extra_clause
    elif team_clause:
        domain = domain + team_clause
    elif extra_clause:
        domain = domain + extra_clause

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
    df["vendedor"] = m2o_name(df["invoice_user_id"]) if "invoice_user_id" in df else "Sin asignar"
    df["cliente"] = m2o_name(df["partner_id"])
    df["linea"] = classify_linea(df)
    return df


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
