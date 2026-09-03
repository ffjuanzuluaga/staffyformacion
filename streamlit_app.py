# -*- coding: utf-8 -*-
"""
Dashboard de las líneas Staff (staffing), Formación y Fábrica de Software,
conectado a Odoo 19 vía XML-RPC. Nace de la reunión con Raquel Cañón / Paula
López del 2026-08 para dejar de depender de Excel en estos informes.

Convenciones (iguales a las de los otros tableros del repo hermano
`gdp-dashboard-1`, que ya corre en producción contra esta misma base de
Odoo):
  - "línea" = crm.team (equipo de venta) — la reunión decidió usar equipos
    en vez de etiquetas para segmentar Staff / Formación / Fábrica de SW.
  - Costos y horas del equipo salen de contabilidad analítica
    (account.analytic.line): se asume UNA cuenta analítica por línea, con
    el mismo nombre que la línea (igual que ya funciona para Soporte /
    Implementación / Licenciamiento en gdp-dashboard-1).
  - Metas de venta: CSV en data/ (igual convención que metas.csv /
    metas_lineas.csv de los otros tableros) — el equipo comercial las
    actualiza sin tocar código.

Supuestos a verificar contra la Odoo real (nombres de campo/modelo que uso
por primera vez aquí, no confirmados aún en este código):
  - "Plazas activas" de Staff = # de sale.order con subscription_state en
    progreso ('3_progress') del equipo Staff. Si una suscripción agrupa
    varias plazas en una sola línea, hay que sumar product_uom_qty en vez
    de contar órdenes — avisen y lo ajusto.
  - Horas del equipo = account.analytic.line (unit_amount, employee_id,
    account_id, date) — es donde caen los partes de horas / timesheets en
    Odoo 19.
  - "Cursos vendidos" (Formación) y "proyectos vendidos" (Fábrica) se miden
    como oportunidades GANADAS por mes de esa línea — la fuente exacta de
    las PROYECCIONES sigue pendiente de definir con Paula (según la
    reunión), así que la proyección a 6 meses es un promedio móvil simple,
    marcado como provisional en la propia UI.
  - "Horas dedicadas a cada tipo de actividad" NO se puede sacar de
    mail.activity (no trae duración en Odoo estándar y Odoo borra la
    actividad al completarla) — queda documentado como pendiente, igual
    que ya está documentado ese límite en tablero_soporte_firefly.

Si algo de esto truena contra la Odoo real, el error dice qué modelo/campo
revisar — no hace falta adivinar, se ajusta con el traceback en mano.
"""

import threading
import xmlrpc.client
from datetime import date, datetime

import pandas as pd
import plotly.express as px
import streamlit as st

# ─────────────────────────────────────────────
# Configuración de página
# ─────────────────────────────────────────────
st.set_page_config(
    page_title="Dashboard Staff y Formación · Odoo 19",
    page_icon="📋",
    layout="wide",
)

# Línea de negocio → nombre del equipo de venta (crm.team) en Odoo. Un solo
# lugar para corregir si el nombre real del equipo cambia.
LINEA_TEAM = {
    "Staff": "Staffing IT",
    "Formación": "FORMACION",
    "Fábrica de Software": "FABRICA SOFTWARE",
}

# Línea de negocio → nombre de la cuenta analítica (account.analytic.account)
# que agrupa sus costos/horas. OJO: el nombre de la cuenta analítica NO es
# igual al del equipo de venta (confirmado con Juan Camilo).
LINEA_ANALYTIC = {
    "Staff": "Staffing IT",
    "Formación": "Formación TI",
    "Fábrica de Software": "Fábrica Software",
}
# Inversa (nombre de cuenta analítica en Odoo → línea interna), para volver a
# la clave interna después de leer account.analytic.line — que trae el
# nombre de la cuenta, no la línea.
ANALYTIC_TO_LINEA = {v: k for k, v in LINEA_ANALYTIC.items()}


def fmt_money(v: float) -> str:
    return f"${v:,.0f}"


# ─────────────────────────────────────────────
# Conexión a Odoo (XML-RPC) — mismo patrón que los demás tableros del repo
# ─────────────────────────────────────────────
_odoo_lock = threading.Lock()


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


def m2o_name(series: pd.Series) -> pd.Series:
    return series.apply(lambda v: v[1] if isinstance(v, (list, tuple)) and len(v) == 2 else "Sin asignar")


def m2o_id(series: pd.Series) -> pd.Series:
    return series.apply(lambda v: v[0] if isinstance(v, (list, tuple)) else None)


# ─────────────────────────────────────────────
# Carga de datos (cacheada 10 min)
# ─────────────────────────────────────────────
@st.cache_data(ttl=600, show_spinner="Cargando equipos de venta...")
def load_teams() -> pd.DataFrame:
    return search_read("crm.team", [], ["id", "name"], order="name")


def team_id_for_linea(teams_df: pd.DataFrame, linea: str) -> int | None:
    nombre = LINEA_TEAM.get(linea)
    match = teams_df.loc[teams_df["name"] == nombre, "id"]
    return int(match.iloc[0]) if not match.empty else None


@st.cache_data(ttl=600, show_spinner="Cargando leads y pipeline...")
def load_leads_full(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Todo el pipeline (leads + oportunidades) con origen y equipo, para
    clasificar por línea, conteo de leads nuevos y análisis de origen."""
    domain = [
        ("create_date", ">=", date_from),
        ("create_date", "<=", f"{date_to} 23:59:59"),
        "|", ("active", "=", True), ("active", "=", False),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    df = search_read(
        "crm.lead", domain,
        ["name", "create_date", "date_closed", "user_id", "team_id", "source_id",
         "expected_revenue", "probability", "active", "type", "won_status"],
        order="create_date",
    )
    if df.empty:
        return df
    df["create_date"] = pd.to_datetime(df["create_date"])
    df["mes"] = df["create_date"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["origen"] = m2o_name(df["source_id"])
    df["estado"] = df["won_status"].map({"won": "Ganada", "lost": "Perdida", "pending": "Abierta"})
    return df


@st.cache_data(ttl=600, show_spinner="Cargando oportunidades ganadas...")
def load_won(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    """Oportunidades GANADAS con fecha de cierre en el período (para
    "cierre de venta mes a mes" — sale del CRM, no de facturación).
    GANADA = won_status == 'won' (probability 100 Y stage_id.is_won — el campo
    real de Odoo, confirmado en odoo/addons/crm/models/crm_lead.py; más estricto
    que solo probability == 100)."""
    domain = [
        ("type", "=", "opportunity"),
        ("won_status", "=", "won"),
        ("date_closed", ">=", date_from),
        ("date_closed", "<=", f"{date_to} 23:59:59"),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    df = search_read(
        "crm.lead", domain,
        ["name", "date_closed", "user_id", "team_id", "partner_id", "expected_revenue"],
        order="date_closed",
    )
    if df.empty:
        return df
    df["date_closed"] = pd.to_datetime(df["date_closed"])
    df["mes"] = df["date_closed"].dt.to_period("M").astype(str)
    df["vendedor"] = m2o_name(df["user_id"])
    df["equipo"] = m2o_name(df["team_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    return df


@st.cache_data(ttl=600, show_spinner="Cargando facturas...")
def load_invoices(date_from: str, date_to: str, team_ids: list[int]) -> pd.DataFrame:
    domain = [
        ("move_type", "in", ["out_invoice", "out_refund"]),
        ("state", "=", "posted"),
        ("invoice_date", ">=", date_from),
        ("invoice_date", "<=", date_to),
    ]
    if team_ids:
        domain.append(("team_id", "in", team_ids))
    df = search_read(
        "account.move", domain,
        ["name", "invoice_date", "partner_id", "invoice_user_id", "team_id",
         "amount_total_signed", "payment_state", "move_type"],
        order="invoice_date",
    )
    if df.empty:
        return df
    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    df["mes"] = df["invoice_date"].dt.to_period("M").astype(str)
    df["equipo"] = m2o_name(df["team_id"])
    df["vendedor"] = m2o_name(df["invoice_user_id"])
    df["cliente"] = m2o_name(df["partner_id"])
    return df


@st.cache_data(ttl=600, show_spinner="Cargando suscripciones de Staff...")
def load_subscriptions(team_id: int | None):
    """Suscripciones (sale.order con subscription_state) del equipo dado —
    fuente de "plazas activas" de Staff. Devuelve (df, error); error trae
    el mensaje si el campo/modelo no coincide con esta instancia."""
    cols = ["name", "partner_id", "subscription_state", "start_date", "end_date", "team_id"]
    domain = [("is_subscription", "=", True)]
    if team_id:
        domain.append(("team_id", "=", team_id))
    try:
        df = search_read("sale.order", domain, cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), (
            f"No se pudo consultar suscripciones (sale.order.is_subscription/subscription_state): {e}. "
            "Puede que en esta versión el campo se llame distinto — revisa el modelo sale.order."
        )
    if df.empty:
        return df, None
    df["cliente"] = m2o_name(df["partner_id"])
    df["start_date"] = pd.to_datetime(df["start_date"], errors="coerce")
    df["end_date"] = pd.to_datetime(df["end_date"], errors="coerce")
    return df, None


def coverage_per_month(df: pd.DataFrame, start_col: str, end_col: str, months: list[str],
                        state_col: str | None = None, active_states: tuple = ()) -> pd.DataFrame:
    """# de filas cuya vigencia [start,end] cubre cada mes de `months`
    (formato 'YYYY-MM'). end nulo = sigue vigente. Mismo criterio de
    "ventana de vigencia" que ya usa tablero_soporte_firefly para contratos
    activos por mes."""
    rows = []
    for mes in months:
        period = pd.Period(mes, freq="M")
        month_start, month_end = period.to_timestamp(how="start"), period.to_timestamp(how="end")
        sub = df
        if state_col and active_states:
            sub = df[df[state_col].isin(active_states)]
        active = (sub[start_col] <= month_end) & (sub[end_col].isna() | (sub[end_col] >= month_start))
        rows.append({"mes": mes, "activas": int(active.sum())})
    return pd.DataFrame(rows)


def naive_projection(monthly_df: pd.DataFrame, value_col: str, months_fwd: int = 6, window: int = 3) -> pd.DataFrame:
    """Proyección simple: promedio móvil de los últimos `window` meses,
    repetido hacia adelante. PROVISIONAL — reemplazar cuando se defina la
    fuente real de proyecciones con Paula (pendiente según la reunión)."""
    if monthly_df.empty:
        return pd.DataFrame(columns=["mes", value_col])
    ultimo_mes = pd.Period(monthly_df["mes"].max(), freq="M")
    promedio = monthly_df.tail(window)[value_col].mean()
    rows = []
    for i in range(1, months_fwd + 1):
        mes = (ultimo_mes + i)
        rows.append({"mes": str(mes), value_col: promedio})
    return pd.DataFrame(rows)


@st.cache_data(ttl=600, show_spinner="Cargando costos analíticos por línea...")
def load_analytic_costs(date_from: str, date_to: str):
    """Costos (importe negativo, convención estándar de Odoo) por cuenta
    analítica de línea — igual patrón que gdp-dashboard-1 usa para
    Soporte/Implementación/Licenciamiento."""
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
            f"No se pudo consultar costos analíticos (account.analytic.line): {e}. "
            f"Verifica que exista una cuenta analítica por línea, con el mismo nombre "
            f"que la línea ({', '.join(LINEA_ANALYTIC.values())})."
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
    """Horas registradas (partes de horas / timesheets, unit_amount > 0)
    por cuenta analítica de línea, empleado y mes."""
    cols = ["date", "unit_amount", "account_id", "employee_id"]
    domain = [
        ("account_id.name", "in", list(LINEA_ANALYTIC.values())),
        ("date", ">=", date_from), ("date", "<=", date_to),
        ("unit_amount", ">", 0),
    ]
    try:
        df = search_read("account.analytic.line", domain, cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo consultar horas del equipo (account.analytic.line.unit_amount/employee_id): {e}"
    if df.empty:
        return df, None
    df["date"] = pd.to_datetime(df["date"])
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["linea"] = m2o_name(df["account_id"]).map(ANALYTIC_TO_LINEA).fillna("Sin línea")
    df["persona"] = m2o_name(df["employee_id"])
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando vendedores de los equipos de venta...")
def load_sales_team_employees(team_ids: list[int]) -> pd.DataFrame:
    """Empleados (hr.employee) que son vendedores (member_ids, res.users) de
    los equipos de venta dados — para acotar las horas por proyecto solo a
    estas personas, no a todo el equipo de delivery/consultoría."""
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
    """Horas (timesheets, unit_amount > 0) por PROYECTO (project.project) —
    no por tarea — empleado y mes. Para el "Top 5 de proyectos" que pidió
    Raquel: monitorear en qué se va el tiempo del equipo comercial, mes a
    mes, para ver si la carga está alineada con lo comercial/operativo."""
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
        return pd.DataFrame(columns=cols), f"No se pudo consultar horas por proyecto (account.analytic.line.project_id): {e}"
    if df.empty:
        return df, None
    df["date"] = pd.to_datetime(df["date"])
    df["mes"] = df["date"].dt.to_period("M").astype(str)
    df["proyecto"] = m2o_name(df["project_id"])
    df["vendedor"] = m2o_name(df["employee_id"])
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando actividades del equipo...")
def load_team_activities(team_ids: list[int]):
    """Actividades PENDIENTES (mail.activity) de crm.lead. Es una FOTO del
    backlog actual, no un histórico — Odoo borra la actividad al marcarla
    hecha (queda como mensaje en el chatter, no como registro consultable
    por mes). Mismo límite ya documentado en tablero_soporte_firefly."""
    cols = ["res_id", "activity_type_id", "user_id", "date_deadline", "create_date"]
    try:
        df = search_read("mail.activity", [("res_model", "=", "crm.lead")], cols)
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo consultar actividades (mail.activity): {e}"
    if df.empty:
        return df, None
    df["tipo"] = m2o_name(df["activity_type_id"])
    df["vendedor"] = m2o_name(df["user_id"])
    df["date_deadline"] = pd.to_datetime(df["date_deadline"])
    df["mes"] = df["date_deadline"].dt.to_period("M").astype(str)

    # Solo se piden los leads referenciados por estas actividades (no toda
    # la tabla crm.lead), para no lanzar un search_read sin dominio sobre
    # una tabla que puede tener miles de registros.
    res_ids = sorted({int(rid) for rid in df["res_id"].dropna().unique()})
    leads_domain = [("id", "in", res_ids)]
    if team_ids:
        leads_domain.append(("team_id", "in", team_ids))
    leads_teams = search_read("crm.lead", leads_domain, ["id", "team_id"]) if res_ids else pd.DataFrame(columns=["id", "team_id"])
    if leads_teams.empty:
        return df.iloc[0:0], None
    leads_teams["equipo"] = m2o_name(leads_teams["team_id"])
    # inner join: si se pidió un team_ids, esto ya deja solo esos equipos;
    # si no se pidió, deja solo actividades cuyo lead se pudo resolver.
    df = df.merge(leads_teams[["id", "equipo"]], left_on="res_id", right_on="id", how="inner")
    return df, None


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
# Sidebar: filtros globales
# ─────────────────────────────────────────────
st.sidebar.title("⚙️ Filtros")

hoy = date.today()
anio = st.sidebar.selectbox("Año a analizar", options=range(hoy.year, hoy.year - 4, -1), index=0)

if st.sidebar.button("🔄 Refrescar datos"):
    st.cache_data.clear()
    st.rerun()

st.sidebar.caption(f"Datos cacheados por 10 min · {datetime.now():%H:%M}")

teams_df = load_teams()
metas_lineas = load_metas_lineas()
costos_fijos = load_costos_fijos()

if metas_lineas.empty or (metas_lineas["meta_anual"] == 0).all():
    st.sidebar.warning("⚠️ Completa `data/metas_lineas.csv` con las metas anuales reales de cada línea.")

d1, d2 = f"{anio}-01-01", f"{anio}-12-31"
desde_12m = (pd.Period(hoy, freq="M") - 11).to_timestamp().date().isoformat()
hoy_iso = hoy.isoformat()

st.title("📋 Dashboard Staff, Formación y Fábrica de Software")
st.caption(f"Año analizado: {anio} · Línea = equipo de venta en Odoo (crm.team) · "
           "Actualiza `data/metas_lineas.csv` y `data/costos_fijos.csv` para afinar cumplimiento y rentabilidad.")


# ─────────────────────────────────────────────
# Helpers compartidos por las pestañas de línea (Staff / Formación / Fábrica)
# ─────────────────────────────────────────────

def kpis_linea(linea: str, team_id: int | None, won_year: pd.DataFrame, invoices_year: pd.DataFrame,
                leads_full_year: pd.DataFrame) -> dict:
    meta_row = metas_lineas.loc[metas_lineas["linea"] == linea, "meta_anual"]
    meta_anual = float(meta_row.iloc[0]) if not meta_row.empty else 0.0
    vendido_anual = won_year["expected_revenue"].sum() if not won_year.empty else 0.0
    facturado_anual = invoices_year["amount_total_signed"].sum() if not invoices_year.empty else 0.0
    pct_cumpl = (vendido_anual / meta_anual * 100) if meta_anual else 0.0
    mes_actual_key = f"{hoy.year}-{hoy.month:02d}"
    leads_mes = leads_full_year.loc[leads_full_year["mes"] == mes_actual_key].shape[0] if not leads_full_year.empty else 0
    return {
        "meta_anual": meta_anual, "vendido_anual": vendido_anual,
        "facturado_anual": facturado_anual, "pct_cumpl": pct_cumpl, "leads_mes": leads_mes,
    }


def rentabilidad_mensual_linea(linea: str, invoices_year: pd.DataFrame, costos_analytic: pd.DataFrame,
                                costos_fijos_df: pd.DataFrame, months: list[str]) -> pd.DataFrame:
    fact_mensual = (invoices_year.groupby("mes", as_index=False)["amount_total_signed"].sum()
                    .rename(columns={"amount_total_signed": "facturado"})) if not invoices_year.empty else pd.DataFrame(columns=["mes", "facturado"])
    costo_mensual = (costos_analytic[costos_analytic["linea"] == linea].groupby("mes", as_index=False)["costo"].sum()
                     if costos_analytic is not None and not costos_analytic.empty else pd.DataFrame(columns=["mes", "costo"]))
    costo_fijo_mensual = costos_fijos_df.loc[costos_fijos_df["linea"] == linea, "costo_mensual"].sum()

    base = pd.DataFrame({"mes": months})
    tabla = base.merge(fact_mensual, on="mes", how="left").merge(costo_mensual, on="mes", how="left")
    tabla["facturado"] = tabla["facturado"].fillna(0)
    tabla["costo"] = tabla["costo"].fillna(0) + costo_fijo_mensual
    tabla["rentabilidad"] = tabla["facturado"] - tabla["costo"]
    return tabla


def render_linea_tab(linea: str, extra_kpi_label: str, extra_kpi_value):
    team_id = team_id_for_linea(teams_df, linea)
    if team_id is None:
        st.warning(f"No encontré un equipo de venta llamado **{LINEA_TEAM[linea]}** en Odoo (crm.team). "
                   f"Ajusta `LINEA_TEAM` en el código si el nombre real es distinto, o créalo en Odoo.")
        team_ids = []
    else:
        team_ids = [team_id]

    won_year = load_won(d1, d2, team_ids)
    invoices_year = load_invoices(d1, d2, team_ids)
    leads_full_year = load_leads_full(d1, d2, team_ids)
    costos_analytic, err_costo = load_analytic_costs(d1, d2)

    k = kpis_linea(linea, team_id, won_year, invoices_year, leads_full_year)
    c0, c1, c2, c3, c4 = st.columns(5)
    c0.metric(extra_kpi_label, extra_kpi_value)
    c1.metric("Cumplimiento meta anual", f"{k['pct_cumpl']:.1f}%")
    c2.metric(f"Vendido en {linea} (año)", fmt_money(k["vendido_anual"]))
    c3.metric("Facturación acumulada (año)", fmt_money(k["facturado_anual"]))
    c4.metric("Leads del mes", k["leads_mes"])

    st.markdown("#### 💰 Cierre de venta mes a mes vs. meta")
    meta_row = metas_lineas.loc[metas_lineas["linea"] == linea, "meta_anual"]
    meta_mensual = (float(meta_row.iloc[0]) / 12) if not meta_row.empty else 0.0
    months_year = [f"{anio}-{m:02d}" for m in range(1, 13)]
    ventas_mes = (won_year.groupby("mes", as_index=False)["expected_revenue"].sum()
                 if not won_year.empty else pd.DataFrame(columns=["mes", "expected_revenue"]))
    base_meses = pd.DataFrame({"mes": months_year})
    ventas_vs_meta = base_meses.merge(ventas_mes, on="mes", how="left")
    ventas_vs_meta["expected_revenue"] = ventas_vs_meta["expected_revenue"].fillna(0)
    ventas_vs_meta["meta_mensual"] = meta_mensual
    largo = ventas_vs_meta.melt(id_vars="mes", value_vars=["meta_mensual", "expected_revenue"],
                                 var_name="concepto", value_name="valor")
    largo["concepto"] = largo["concepto"].map({"meta_mensual": "Meta mensual", "expected_revenue": "Vendido"})
    fig = px.bar(largo, x="mes", y="valor", color="concepto", barmode="group",
                 title=f"{linea} — vendido vs. meta mensual ({anio})",
                 color_discrete_map={"Meta mensual": "#9ca3af", "Vendido": "#1f77b4"},
                 labels={"valor": "COP", "mes": "Mes"})
    st.plotly_chart(fig, use_container_width=True)

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("#### 🧾 Facturación mes a mes")
        if invoices_year.empty:
            st.info("No hay facturas en el período.")
        else:
            fact_mes = invoices_year.groupby("mes", as_index=False)["amount_total_signed"].sum()
            fig = px.bar(fact_mes, x="mes", y="amount_total_signed", text_auto=".2s",
                         title=f"{linea} — facturación por mes ({anio})",
                         labels={"amount_total_signed": "COP", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
    with col_b:
        st.markdown("#### 📨 Leads mes a mes")
        if leads_full_year.empty:
            st.info("No hay leads en el período.")
        else:
            leads_mes_df = leads_full_year.groupby("mes", as_index=False).agg(leads=("name", "count"))
            fig = px.bar(leads_mes_df, x="mes", y="leads", text_auto=True,
                         title=f"{linea} — leads nuevos por mes ({anio})",
                         labels={"leads": "Leads", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

    st.markdown("#### 🌐 Leads por origen y mes")
    if leads_full_year.empty:
        st.info("No hay leads en el período.")
    else:
        origen_mes = leads_full_year.groupby(["mes", "origen"], as_index=False).agg(leads=("name", "count"))
        fig = px.bar(origen_mes, x="mes", y="leads", color="origen", barmode="stack",
                     title=f"{linea} — leads por origen y mes ({anio})",
                     labels={"leads": "Leads", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("#### 📈 Rentabilidad mensual")
    st.caption("Facturado − costos de proveedores/equipo (cuenta analítica) − costo fijo asignado "
               "a la línea (`data/costos_fijos.csv`). Variación mes a mes, no acumulada.")
    if err_costo:
        st.warning(err_costo)
    rent = rentabilidad_mensual_linea(linea, invoices_year, costos_analytic, costos_fijos, months_year)
    fig = px.bar(rent, x="mes", y="rentabilidad", text_auto=".2s",
                 title=f"{linea} — rentabilidad mensual ({anio})",
                 labels={"rentabilidad": "COP", "mes": "Mes"})
    st.plotly_chart(fig, use_container_width=True)
    with st.expander("📋 Detalle de rentabilidad mensual"):
        st.dataframe(
            rent, use_container_width=True, hide_index=True,
            column_config={
                "mes": "Mes",
                "facturado": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                "costo": st.column_config.NumberColumn("Costo (analítico + fijo)", format="$%,.0f"),
                "rentabilidad": st.column_config.NumberColumn("Rentabilidad", format="$%,.0f"),
            },
        )

    with st.expander("📋 Detalle de oportunidades ganadas del año"):
        if won_year.empty:
            st.caption("Sin oportunidades ganadas en el período.")
        else:
            st.dataframe(won_year[["name", "date_closed", "vendedor", "cliente", "expected_revenue"]],
                         use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────
# Pestañas
# ─────────────────────────────────────────────
tab_resumen, tab_staff, tab_formacion, tab_fabrica, tab_equipo, tab_vendedor = st.tabs(
    ["🏠 Resumen", "👥 Staff", "🎓 Formación", "💻 Fábrica de Software", "🕐 Equipo", "🏆 Vendedores"]
)

# --- Resumen ejecutivo -------------------------------------------------
with tab_resumen:
    st.caption("Comparativo de las 3 líneas. Usa el año seleccionado en la barra lateral.")

    won_all_lineas = []
    fact_all_lineas = []
    leads_all_lineas = []
    for linea in LINEA_TEAM:
        tid = team_id_for_linea(teams_df, linea)
        ids = [tid] if tid else []
        w = load_won(d1, d2, ids)
        if not w.empty:
            w = w.assign(linea=linea)
            won_all_lineas.append(w)
        f = load_invoices(d1, d2, ids)
        if not f.empty:
            f = f.assign(linea=linea)
            fact_all_lineas.append(f)
        l = load_leads_full(d1, d2, ids)
        if not l.empty:
            l = l.assign(linea=linea)
            leads_all_lineas.append(l)

    won_all = pd.concat(won_all_lineas, ignore_index=True) if won_all_lineas else pd.DataFrame(columns=["mes", "linea", "expected_revenue"])
    fact_all = pd.concat(fact_all_lineas, ignore_index=True) if fact_all_lineas else pd.DataFrame(columns=["mes", "linea", "amount_total_signed"])
    leads_all = pd.concat(leads_all_lineas, ignore_index=True) if leads_all_lineas else pd.DataFrame(columns=["mes", "linea", "name"])

    st.markdown("#### 🎯 Cumplimiento de meta anual por línea")
    resumen_rows = []
    for linea in LINEA_TEAM:
        meta_row = metas_lineas.loc[metas_lineas["linea"] == linea, "meta_anual"]
        meta_anual = float(meta_row.iloc[0]) if not meta_row.empty else 0.0
        vendido = won_all.loc[won_all["linea"] == linea, "expected_revenue"].sum() if not won_all.empty else 0.0
        facturado = fact_all.loc[fact_all["linea"] == linea, "amount_total_signed"].sum() if not fact_all.empty else 0.0
        resumen_rows.append({
            "linea": linea, "meta_anual": meta_anual, "vendido": vendido, "facturado": facturado,
            "pct_cumpl": (vendido / meta_anual * 100) if meta_anual else 0.0,
        })
    resumen_df = pd.DataFrame(resumen_rows)
    st.dataframe(
        resumen_df, use_container_width=True, hide_index=True,
        column_config={
            "linea": "Línea",
            "meta_anual": st.column_config.NumberColumn("Meta año", format="$%,.0f"),
            "vendido": st.column_config.NumberColumn("Vendido", format="$%,.0f"),
            "facturado": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
            "pct_cumpl": st.column_config.ProgressColumn("% Cumplimiento", format="%.1f%%", min_value=0, max_value=150),
        },
    )

    col1, col2 = st.columns(2)
    with col1:
        if not fact_all.empty:
            mensual = fact_all.groupby(["mes", "linea"], as_index=False)["amount_total_signed"].sum()
            fig = px.bar(mensual, x="mes", y="amount_total_signed", color="linea", barmode="group",
                         title="Facturación mes a mes por línea", labels={"amount_total_signed": "COP", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
    with col2:
        if not won_all.empty:
            mensual_v = won_all.groupby(["mes", "linea"], as_index=False)["expected_revenue"].sum()
            fig = px.bar(mensual_v, x="mes", y="expected_revenue", color="linea", barmode="group",
                         title="Ventas ganadas mes a mes por línea", labels={"expected_revenue": "COP", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

    if not leads_all.empty:
        mes_actual_key = f"{hoy.year}-{hoy.month:02d}"
        leads_mes_linea = (leads_all[leads_all["mes"] == mes_actual_key].groupby("linea", as_index=False)
                           .agg(leads=("name", "count")))
        col3, col4 = st.columns(2)
        with col3:
            fig = px.bar(leads_mes_linea, x="linea", y="leads", text_auto=True,
                         title="Leads del mes actual por línea", labels={"leads": "Leads", "linea": "Línea"})
            st.plotly_chart(fig, use_container_width=True)
        with col4:
            abiertas_linea = (leads_all[leads_all["estado"] == "Abierta"].groupby("linea", as_index=False)
                              .agg(pipeline=("expected_revenue", "sum")))
            fig = px.bar(abiertas_linea, x="linea", y="pipeline", text_auto=".2s",
                         title="Pipeline abierto por línea", labels={"pipeline": "COP", "linea": "Línea"})
            st.plotly_chart(fig, use_container_width=True)

    st.markdown("#### 👥 Plazas activas de Staff, mes a mes (últimos 12 meses)")
    staff_team_id = team_id_for_linea(teams_df, "Staff")
    subs_df, err_subs = load_subscriptions(staff_team_id)
    if err_subs:
        st.warning(err_subs)
    elif subs_df.empty:
        st.info("No hay suscripciones activas de Staff para calcular plazas.")
    else:
        meses_12 = [str(pd.Period(desde_12m, freq="M") + i) for i in range(12)]
        plazas_12m = coverage_per_month(subs_df, "start_date", "end_date", meses_12,
                                         state_col="subscription_state", active_states=("3_progress",))
        fig = px.bar(plazas_12m, x="mes", y="activas", text_auto=True,
                     title="Plazas activas de Staff por mes", labels={"activas": "Plazas", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)


# --- Staff --------------------------------------------------------------
with tab_staff:
    staff_team_id = team_id_for_linea(teams_df, "Staff")
    subs_df, err_subs = load_subscriptions(staff_team_id)

    plazas_actuales = 0
    if err_subs:
        st.warning(err_subs)
    elif not subs_df.empty:
        plazas_actuales = int((subs_df["subscription_state"] == "3_progress").sum())

    render_linea_tab("Staff", "Plazas activas actualmente", plazas_actuales)

    if not err_subs and not subs_df.empty:
        st.divider()
        st.markdown("#### 📈 Tendencia de plazas activas (últimos 12 meses) y proyección (próximos 6)")
        meses_12 = [str(pd.Period(desde_12m, freq="M") + i) for i in range(12)]
        historico = coverage_per_month(subs_df, "start_date", "end_date", meses_12,
                                        state_col="subscription_state", active_states=("3_progress",))
        meses_6fwd = [str(pd.Period(hoy, freq="M") + i) for i in range(1, 7)]
        proyeccion = coverage_per_month(subs_df, "start_date", "end_date", meses_6fwd,
                                         state_col="subscription_state", active_states=("3_progress",))
        historico["tipo"] = "Histórico"
        proyeccion["tipo"] = "Proyección (plazas ya vendidas con fin definido)"
        combinado = pd.concat([historico, proyeccion], ignore_index=True)
        fig = px.line(combinado, x="mes", y="activas", color="tipo", markers=True,
                     title="Plazas activas — histórico y proyección",
                     labels={"activas": "Plazas", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        st.caption("La proyección solo refleja plazas YA vendidas con fecha de fin definida en la "
                   "suscripción; no incluye ventas futuras aún no cerradas.")
        with st.expander("📋 Detalle de suscripciones de Staff"):
            st.dataframe(
                subs_df[["name", "cliente", "subscription_state", "start_date", "end_date"]],
                use_container_width=True, hide_index=True,
            )
    else:
        st.info("Sin datos de suscripciones para graficar tendencia/proyección de plazas.")


# --- Formación ------------------------------------------------------------
with tab_formacion:
    formacion_team_id = team_id_for_linea(teams_df, "Formación")
    won_year_formacion = load_won(d1, d2, [formacion_team_id] if formacion_team_id else [])
    cursos_year = won_year_formacion.shape[0] if not won_year_formacion.empty else 0

    render_linea_tab("Formación", "Cursos entregados en el año", cursos_year)

    st.divider()
    st.markdown("#### 📈 Cursos vendidos (últimos 12 meses) y proyección (próximos 6, provisional)")
    st.caption("La proyección es un promedio móvil simple de los últimos 3 meses — placeholder hasta "
               "definir la fuente real de proyecciones de cursos con Paula (pendiente de la reunión).")
    won_12m_formacion = load_won(desde_12m, hoy_iso, [formacion_team_id] if formacion_team_id else [])
    if won_12m_formacion.empty:
        st.info("No hay cursos (oportunidades ganadas) vendidos en los últimos 12 meses.")
    else:
        cursos_mes = won_12m_formacion.groupby("mes", as_index=False).agg(cursos=("name", "count"))
        proy = naive_projection(cursos_mes, "cursos", months_fwd=6, window=3)
        cursos_mes["tipo"] = "Histórico"
        proy["tipo"] = "Proyección (provisional)"
        combinado = pd.concat([cursos_mes, proy], ignore_index=True)
        fig = px.bar(combinado, x="mes", y="cursos", color="tipo",
                     title="Cursos vendidos por mes — histórico y proyección",
                     labels={"cursos": "Cursos", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)


# --- Fábrica de Software --------------------------------------------------
with tab_fabrica:
    fabrica_team_id = team_id_for_linea(teams_df, "Fábrica de Software")
    won_year_fabrica = load_won(d1, d2, [fabrica_team_id] if fabrica_team_id else [])
    proyectos_year = won_year_fabrica.shape[0] if not won_year_fabrica.empty else 0

    render_linea_tab("Fábrica de Software", "Proyectos acumulados del año", proyectos_year)

    st.divider()
    st.markdown("#### 📈 Proyectos vendidos (últimos 12 meses) y proyección (próximos 6, provisional)")
    st.caption("Igual que en Formación: proyección = promedio móvil simple, placeholder hasta definir "
               "la fuente real de proyecciones.")
    won_12m_fabrica = load_won(desde_12m, hoy_iso, [fabrica_team_id] if fabrica_team_id else [])
    if won_12m_fabrica.empty:
        st.info("No hay proyectos (oportunidades ganadas) vendidos en los últimos 12 meses.")
    else:
        proyectos_mes = won_12m_fabrica.groupby("mes", as_index=False).agg(proyectos=("name", "count"))
        proy = naive_projection(proyectos_mes, "proyectos", months_fwd=6, window=3)
        proyectos_mes["tipo"] = "Histórico"
        proy["tipo"] = "Proyección (provisional)"
        combinado = pd.concat([proyectos_mes, proy], ignore_index=True)
        fig = px.bar(combinado, x="mes", y="proyectos", color="tipo",
                     title="Proyectos vendidos por mes — histórico y proyección",
                     labels={"proyectos": "Proyectos", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)


# --- Equipo (horas y actividades) -----------------------------------------
with tab_equipo:
    team_ids_equipo = [tid for tid in (team_id_for_linea(teams_df, l) for l in LINEA_TEAM) if tid]

    st.markdown("#### ⏱️ Horas del equipo por persona, mes y línea")
    horas_df, err_horas = load_analytic_hours(d1, d2)
    if err_horas:
        st.warning(err_horas)
    elif horas_df.empty:
        st.info("No hay horas registradas (account.analytic.line) en el año seleccionado.")
    else:
        col1, col2 = st.columns(2)
        with col1:
            horas_linea_mes = horas_df.groupby(["mes", "linea"], as_index=False)["unit_amount"].sum()
            fig = px.bar(horas_linea_mes, x="mes", y="unit_amount", color="linea", barmode="group",
                         title="Horas del equipo por mes y línea",
                         labels={"unit_amount": "Horas", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
        with col2:
            horas_persona = (horas_df.groupby("persona", as_index=False)["unit_amount"].sum()
                             .sort_values("unit_amount", ascending=True).tail(15))
            fig = px.bar(horas_persona, x="unit_amount", y="persona", orientation="h",
                         title="Horas totales por persona (año)", text_auto=".1f",
                         labels={"unit_amount": "Horas", "persona": ""})
            st.plotly_chart(fig, use_container_width=True)

        st.markdown("##### Detalle: horas por persona, mes y línea")
        pivot_horas = horas_df.pivot_table(index="persona", columns=["linea", "mes"], values="unit_amount",
                                            aggfunc="sum", fill_value=0)
        with st.expander("📋 Ver tabla completa"):
            st.dataframe(pivot_horas, use_container_width=True)

    st.divider()
    st.markdown("#### 🏗️ Top 5 proyectos por horas — vendedores de Staff / Formación / Fábrica (histórico mensual)")
    st.caption("Por PROYECTO (project.project), no por tarea — solo horas de quienes son miembros de "
               "los 3 equipos de venta. Para ver mes a mes en qué se les va el tiempo y cómo cambia esa "
               "asignación (ej. si un vendedor se corre de 'Gestión comercial' hacia otra cosa).")

    vendedores_df = load_sales_team_employees(team_ids_equipo)
    if vendedores_df.empty:
        st.info("No encontré vendedores (miembros de los equipos Staff/Formación/Fábrica en crm.team) "
                "para cruzar con las horas registradas.")
    else:
        horas_proy_df, err_proy = load_hours_by_project(d1, d2, vendedores_df["employee_id"].tolist())
        if err_proy:
            st.warning(err_proy)
        elif horas_proy_df.empty:
            st.info("No hay horas por proyecto registradas para estos vendedores en el año seleccionado.")
        else:
            resumen_proy = (horas_proy_df.groupby("proyecto", as_index=False)["unit_amount"].sum()
                            .sort_values("unit_amount", ascending=False))
            total_horas_proy = resumen_proy["unit_amount"].sum()
            resumen_proy["pct"] = resumen_proy["unit_amount"] / total_horas_proy * 100 if total_horas_proy else 0
            top5_proyectos = resumen_proy.head(5)["proyecto"].tolist()

            st.markdown("##### Todos los proyectos — horas y % del total (año seleccionado)")
            st.dataframe(
                resumen_proy, use_container_width=True, hide_index=True,
                column_config={
                    "proyecto": "Proyecto",
                    "unit_amount": st.column_config.NumberColumn("Horas", format="%.2f"),
                    "pct": st.column_config.NumberColumn("%", format="%.2f%%"),
                },
            )

            top5_df = horas_proy_df[horas_proy_df["proyecto"].isin(top5_proyectos)]
            mensual_top5 = top5_df.groupby(["mes", "proyecto"], as_index=False)["unit_amount"].sum()
            fig = px.line(mensual_top5, x="mes", y="unit_amount", color="proyecto", markers=True,
                         title="Top 5 proyectos — horas por mes (histórico)",
                         labels={"unit_amount": "Horas", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)

            st.markdown("##### Asignación mensual por vendedor, dentro del Top 5 de proyectos")
            pivot_vend_proy = top5_df.pivot_table(index="vendedor", columns=["mes", "proyecto"],
                                                   values="unit_amount", aggfunc="sum", fill_value=0)
            with st.expander("📋 Ver detalle por vendedor, mes y proyecto"):
                st.dataframe(pivot_vend_proy, use_container_width=True)

    st.divider()
    st.markdown("#### 👥 Actividades del equipo por línea y mes (backlog actual)")
    st.caption("⚠️ Odoo elimina `mail.activity` al marcarla como hecha (queda como mensaje en el "
               "chatter, no como registro consultable por mes). Esto es una **foto del backlog "
               "pendiente hoy**, agrupado por mes de vencimiento — no un histórico de actividades "
               "ya completadas. Tampoco trae horas: `mail.activity` no registra duración en Odoo "
               "estándar, así que \"horas dedicadas a cada actividad\" no se puede sacar de aquí — "
               "necesitaría un registro dedicado (ej. timesheet por tipo de actividad).")

    actividades, err_act = load_team_activities(team_ids_equipo)
    if err_act:
        st.warning(err_act)
    elif actividades.empty:
        st.info("No hay actividades pendientes sobre oportunidades en este momento.")
    else:
        col3, col4 = st.columns(2)
        with col3:
            por_linea_mes = actividades.groupby(["mes", "equipo"], as_index=False).agg(cantidad=("res_id", "count"))
            fig = px.bar(por_linea_mes, x="mes", y="cantidad", color="equipo", barmode="group",
                         title="Actividades pendientes por línea y mes de vencimiento",
                         labels={"cantidad": "Actividades", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
        with col4:
            por_tipo = actividades.groupby("tipo", as_index=False).agg(cantidad=("res_id", "count"))
            fig = px.bar(por_tipo.sort_values("cantidad"), x="cantidad", y="tipo", orientation="h",
                         title="Actividades pendientes por tipo", text_auto=True,
                         labels={"cantidad": "Actividades", "tipo": ""})
            st.plotly_chart(fig, use_container_width=True)
        with st.expander("📋 Detalle de actividades pendientes"):
            st.dataframe(
                actividades[["tipo", "vendedor", "equipo", "date_deadline", "create_date"]].sort_values("date_deadline"),
                use_container_width=True, hide_index=True,
            )


# --- Vendedores -------------------------------------------------------
with tab_vendedor:
    st.caption("Cierres/nuevos negocios y facturación por vendedor, mes y línea — año seleccionado en la barra lateral.")

    won_vend_all = []
    fact_vend_all = []
    for linea in LINEA_TEAM:
        tid = team_id_for_linea(teams_df, linea)
        ids = [tid] if tid else []
        w = load_won(d1, d2, ids)
        if not w.empty:
            won_vend_all.append(w.assign(linea=linea))
        f = load_invoices(d1, d2, ids)
        if not f.empty:
            fact_vend_all.append(f.assign(linea=linea))

    won_vend = pd.concat(won_vend_all, ignore_index=True) if won_vend_all else pd.DataFrame(columns=["mes", "linea", "vendedor", "name", "expected_revenue"])
    fact_vend = pd.concat(fact_vend_all, ignore_index=True) if fact_vend_all else pd.DataFrame(columns=["mes", "linea", "vendedor", "amount_total_signed"])

    st.markdown("#### 🏆 Número de cierres (nuevos negocios) por vendedor, mes y línea")
    if won_vend.empty:
        st.info("No hay oportunidades ganadas en el período.")
    else:
        cierres = won_vend.groupby(["mes", "vendedor", "linea"], as_index=False).agg(cierres=("name", "count"))
        fig = px.bar(cierres, x="mes", y="cierres", color="vendedor", barmode="group", facet_col="linea",
                     title="Cierres por vendedor, mes y línea", labels={"cierres": "Cierres", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        with st.expander("📋 Detalle de cierres"):
            st.dataframe(cierres.sort_values(["linea", "mes"]), use_container_width=True, hide_index=True)

    st.markdown("#### 🧾 Facturación por vendedor, mes y línea")
    if fact_vend.empty:
        st.info("No hay facturas en el período.")
    else:
        fact_v = fact_vend.groupby(["mes", "vendedor", "linea"], as_index=False)["amount_total_signed"].sum()
        fig = px.bar(fact_v, x="mes", y="amount_total_signed", color="vendedor", barmode="group", facet_col="linea",
                     title="Facturación por vendedor, mes y línea",
                     labels={"amount_total_signed": "COP", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        with st.expander("📋 Detalle de facturación"):
            st.dataframe(
                fact_v.sort_values(["linea", "mes"]), use_container_width=True, hide_index=True,
                column_config={"amount_total_signed": st.column_config.NumberColumn("Facturado", format="$%,.0f")},
            )
