# -*- coding: utf-8 -*-
"""
Dashboard de las líneas Staff, Formación y Fábrica de Software.
Conectado a Odoo 19 vía XML-RPC. Extrae CRM, ventas, facturas,
firefly.staffing.request, suscripciones, proyectos (service_line),
analítica y crm.activity.report.

Las 6 preguntas del informe están contestadas en la pestaña Resumen.
"""

from datetime import date, datetime

import pandas as pd
import plotly.express as px
import streamlit as st

from odoo_io import (
    LINEA_ANALYTIC,
    LINEA_TEAM,
    all_team_ids,
    es_tipo_comercial,
    invoice_ids_from_sales,
    load_activity_report,
    load_analytic_costs,
    load_analytic_hours,
    load_calendar_hours,
    load_costos_fijos,
    load_hours_by_project,
    load_invoices,
    load_leads_full,
    load_metas_lineas,
    load_open_pipeline,
    load_projects,
    load_sales,
    load_sales_team_employees,
    load_staffing_renewals,
    load_staffing_requests,
    load_subscription_logs,
    load_subscriptions,
    load_team_activities,
    load_teams,
    load_won,
    month_list,
    resolve_linea_teams,
    staffing_coverage,
    staffing_pnl_monthly,
    subscription_coverage,
    team_id_for_linea,
)

st.set_page_config(
    page_title="Dashboard Staff y Formación · Odoo 19",
    page_icon="📋",
    layout="wide",
)


# Todos los pesos del tablero son ANTES DE IMPUESTOS y en moneda compañía (COP).
# Vendido = amount_untaxed_company (fallback amount_untaxed).
# Facturado = amount_untaxed_signed (ya viene en moneda compañía; NC restan).
SALES_COL = "amount_untaxed_company"
SALES_COL_FALLBACK = "amount_untaxed"
FACT_COL = "amount_untaxed_signed"


def fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def sales_amount_col(df: pd.DataFrame) -> str:
    if df is not None and not df.empty and SALES_COL in df.columns:
        return SALES_COL
    return SALES_COL_FALLBACK


def filtro_linea(df: pd.DataFrame, linea: str) -> pd.DataFrame:
    if df is None or df.empty or "linea" not in df.columns:
        return df if df is not None else pd.DataFrame()
    return df[df["linea"] == linea].copy()


def meta_anual_de(metas: pd.DataFrame, linea: str) -> float:
    row = metas.loc[metas["linea"] == linea, "meta_anual"]
    return float(row.iloc[0]) if not row.empty else 0.0


def costo_fijo_de(costos: pd.DataFrame, linea: str) -> float:
    return float(costos.loc[costos["linea"] == linea, "costo_mensual"].sum()) if not costos.empty else 0.0


def semaforo(pct: float) -> str:
    if pct >= 100:
        return "🟢"
    if pct >= 70:
        return "🟡"
    return "🔴"


# ─────────────────────────────────────────────
# Sidebar
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
# Primero descubrir equipos (crm.team + fallback desde OV). Luego armar ids.
resolved_teams = resolve_linea_teams()
team_ids = [v["id"] for v in resolved_teams.values()]
if not team_ids:
    team_ids = all_team_ids(teams_df)

if metas_lineas.empty or (metas_lineas["meta_anual"] == 0).all():
    st.sidebar.warning("Completa `data/metas_lineas.csv` con las metas anuales reales.")
if not costos_fijos.empty and (costos_fijos["costo_mensual"] == 0).all():
    st.sidebar.info("Los costos fijos de Diego/Paula están en 0. Actualiza `data/costos_fijos.csv` cuando Raquel confirme el valor.")

missing_teams = [LINEA_TEAM[l] for l in LINEA_TEAM if l not in resolved_teams]
if resolved_teams:
    st.sidebar.caption(
        "Equipos mapeados: "
        + ", ".join(
            f"{k}→{v['name']} (id={v['id']}, via={v.get('via', '?')})"
            for k, v in resolved_teams.items()
        )
    )

d1, d2 = f"{anio}-01-01", f"{anio}-12-31"
desde_12m = (pd.Period(hoy, freq="M") - 11).to_timestamp().date().isoformat()
hoy_iso = hoy.isoformat()
months_year = [f"{anio}-{m:02d}" for m in range(1, 13)]
meses_12 = month_list(desde_12m, 12)
meses_6fwd = [str(pd.Period(hoy, freq="M") + i) for i in range(1, 7)]
mes_actual_key = f"{hoy.year}-{hoy.month:02d}"

# ─────────────────────────────────────────────
# Carga única (compartida por todas las pestañas)
# ─────────────────────────────────────────────
sales_all = load_sales(d1, d2, team_ids)
if sales_all.empty:
    st.error(
        "No se trajeron órdenes de venta confirmadas. "
        f"Equipos resueltos: {team_ids or 'ninguno'}. "
        "Prueba 🔄 Refrescar datos. Si sigue vacío, el dominio de OV está fallando contra Odoo."
    )
else:
    # Validación real por datos cargados: si una línea aparece en OV,
    # no debe mostrarse como faltante aunque crm.team no sea visible.
    lineas_en_ov = set(sales_all["linea"].dropna().astype(str).unique()) if "linea" in sales_all.columns else set()
    still_missing = []
    for linea, team_name in LINEA_TEAM.items():
        if linea in lineas_en_ov:
            continue
        if linea in resolved_teams:
            continue
        still_missing.append(team_name)
    if still_missing:
        st.sidebar.warning(
            "No se pudo mapear por crm.team y tampoco apareció en OV: "
            + ", ".join(still_missing)
        )

    # Diagnóstico: conteo por nombre crudo de equipo (antes de clasificar línea)
    group_cols = [c for c in ("equipo", "equipo_id", "linea") if c in sales_all.columns]
    por_equipo = (
        sales_all.groupby(group_cols, as_index=False, dropna=False)
        .agg(ov=("name", "count"), vendido=(sales_amount_col(sales_all), "sum"))
        if group_cols else pd.DataFrame()
    )
    with st.sidebar.expander("OV cargadas por equipo (debug)"):
        if por_equipo.empty:
            st.caption("Sin desglose.")
        else:
            st.dataframe(por_equipo, use_container_width=True, hide_index=True)
            mask = por_equipo["equipo"].astype(str).str.contains("FABRICA", case=False, na=False)
            if "linea" in por_equipo.columns:
                mask = mask | (por_equipo["linea"] == "Fábrica de Software")
            if "equipo_id" in por_equipo.columns:
                mask = mask | (pd.to_numeric(por_equipo["equipo_id"], errors="coerce") == 4)
            fabs = por_equipo[mask]
            if fabs.empty:
                st.error("Ninguna OV de Fábrica (nombre FABRICA, línea o team_id=4).")
            else:
                st.success(f"Fábrica detectada: {int(fabs['ov'].sum())} OV.")

if not sales_all.empty and "fx_sin_trm" in sales_all.columns:
    fx_bad = sales_all[sales_all["fx_sin_trm"]]
    if not fx_bad.empty:
        st.warning(
            f"**{len(fx_bad)} OV en moneda extranjera sin TRM usable** "
            f"(ej. USD con `currency_rate` = 1). "
            f"Quedan sin convertir a COP y distorsionan el total. "
            f"Pedidos: {', '.join(fx_bad['name'].astype(str).head(8).tolist())}"
            + ("…" if len(fx_bad) > 8 else "")
            + ". En Odoo: Contabilidad → Configuración → Monedas → tasas (TRM) "
            "para la fecha del pedido, o corrige la tasa en la OV."
        )
won_all = load_won(d1, d2, team_ids)
won_12m = load_won(desde_12m, hoy_iso, team_ids)
leads_all = load_leads_full(d1, d2, team_ids)
pipeline = load_open_pipeline(team_ids)
inv_extra = invoice_ids_from_sales(sales_all)
invoices_all = load_invoices(d1, d2, team_ids, extra_ids=inv_extra or None)
# Reclasificar facturas sin equipo usando la OV de origen
if not invoices_all.empty and not sales_all.empty and "invoice_ids" in sales_all.columns:
    so_linea = {}
    for _, row in sales_all.iterrows():
        for iid in (row.get("invoice_ids") or []):
            so_linea[int(iid)] = row["linea"]
    if so_linea and "id" in invoices_all.columns:
        from_so = invoices_all["id"].map(so_linea)
        invoices_all["linea"] = invoices_all["linea"].where(
            invoices_all["linea"] != "Sin línea", from_so
        ).fillna(invoices_all["linea"])

costos_analytic, err_costo = load_analytic_costs(d1, d2)
staff_req, err_staff = load_staffing_requests()
staff_team_id = team_id_for_linea(teams_df, "Staff")
subs_df, err_subs = load_subscriptions(staff_team_id)
renewals, err_ren = load_staffing_renewals(d1, d2)
sub_logs, err_logs = load_subscription_logs(d1, d2, team_ids)
projects, err_proj = load_projects(d1, d2)

st.title("📋 Dashboard Staff, Formación y Fábrica de Software")
st.caption(
    f"Año {anio} · montos **antes de impuestos** · "
    f"línea = equipo CRM ({', '.join(LINEA_TEAM.values())}) "
    f"o `service_line` / solicitud Staff · "
    f"cuentas analíticas: {', '.join(LINEA_ANALYTIC.values())}"
)


# ─────────────────────────────────────────────
# Bloques reutilizables
# ─────────────────────────────────────────────
def kpis_linea(linea: str) -> dict:
    sales = filtro_linea(sales_all, linea)
    invoices = filtro_linea(invoices_all, linea)
    leads = filtro_linea(leads_all, linea)
    meta = meta_anual_de(metas_lineas, linea)
    scol = sales_amount_col(sales)
    vendido = float(sales[scol].sum()) if not sales.empty and scol in sales else 0.0
    facturado = float(invoices[FACT_COL].sum()) if not invoices.empty and FACT_COL in invoices else 0.0
    leads_mes = int(leads.loc[leads["mes"] == mes_actual_key].shape[0]) if not leads.empty else 0
    return {
        "meta_anual": meta,
        "vendido_anual": vendido,
        "facturado_anual": facturado,
        "pct_cumpl": (vendido / meta * 100) if meta else 0.0,
        "leads_mes": leads_mes,
        "sales": sales,
        "invoices": invoices,
        "leads": leads,
        "won": filtro_linea(won_all, linea),
    }


def rentabilidad_contable(linea: str) -> pd.DataFrame:
    invoices = filtro_linea(invoices_all, linea)
    fact_mensual = (
        invoices.groupby("mes", as_index=False)[FACT_COL].sum()
        .rename(columns={FACT_COL: "facturado"})
        if not invoices.empty and FACT_COL in invoices else pd.DataFrame(columns=["mes", "facturado"])
    )
    costo_m = (
        costos_analytic[costos_analytic["linea"] == linea].groupby("mes", as_index=False)["costo"].sum()
        if costos_analytic is not None and not costos_analytic.empty
        else pd.DataFrame(columns=["mes", "costo"])
    )
    fijo = costo_fijo_de(costos_fijos, linea)
    tabla = pd.DataFrame({"mes": months_year}).merge(fact_mensual, on="mes", how="left").merge(costo_m, on="mes", how="left")
    tabla["facturado"] = tabla["facturado"].fillna(0)
    tabla["costo"] = tabla["costo"].fillna(0) + fijo
    tabla["rentabilidad"] = tabla["facturado"] - tabla["costo"]
    return tabla


def chart_venta_vs_meta(linea: str, sales: pd.DataFrame):
    st.markdown("#### 💰 Cierre de venta mes a mes vs. meta (antes de impuestos)")
    meta_m = meta_anual_de(metas_lineas, linea) / 12
    scol = sales_amount_col(sales)
    ventas_mes = (
        sales.groupby("mes", as_index=False)[scol].sum()
        if not sales.empty and scol in sales else pd.DataFrame(columns=["mes", scol])
    )
    base = pd.DataFrame({"mes": months_year}).merge(ventas_mes, on="mes", how="left")
    base[scol] = base[scol].fillna(0)
    base["meta_mensual"] = meta_m
    largo = base.melt(id_vars="mes", value_vars=["meta_mensual", scol],
                      var_name="concepto", value_name="valor")
    largo["concepto"] = largo["concepto"].map({"meta_mensual": "Meta mensual", scol: "Vendido"})
    fig = px.bar(
        largo, x="mes", y="valor", color="concepto", barmode="group",
        title=f"{linea} — vendido s/imp. (OV) vs. meta mensual ({anio})",
        color_discrete_map={"Meta mensual": "#9ca3af", "Vendido": "#1f77b4"},
        labels={"valor": "COP s/imp.", "mes": "Mes"},
    )
    st.plotly_chart(fig, use_container_width=True)


def chart_fact_y_leads(linea: str, invoices: pd.DataFrame, leads: pd.DataFrame):
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("#### 🧾 Facturación mes a mes (antes de impuestos)")
        if invoices.empty:
            st.info("No hay facturas clasificadas en esta línea.")
        else:
            fact_mes = invoices.groupby("mes", as_index=False)[FACT_COL].sum()
            fig = px.bar(fact_mes, x="mes", y=FACT_COL, text_auto=".2s",
                         title=f"{linea} — facturación s/imp. ({anio})",
                         labels={FACT_COL: "COP s/imp.", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
    with col_b:
        st.markdown("#### 📨 Leads mes a mes")
        if leads.empty:
            st.info("No hay leads de este equipo. Paula: revisar asignación de equipo CRM.")
        else:
            leads_mes = leads.groupby("mes", as_index=False).agg(leads=("name", "count"))
            fig = px.bar(leads_mes, x="mes", y="leads", text_auto=True,
                         title=f"{linea} — leads nuevos ({anio})",
                         labels={"leads": "Leads", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)


def chart_leads_origen(linea: str, leads: pd.DataFrame):
    st.markdown("#### 🌐 Leads por origen y mes")
    if leads.empty:
        st.info("No hay leads en el período.")
        return
    origen_mes = leads.groupby(["mes", "origen"], as_index=False).agg(leads=("name", "count"))
    fig = px.bar(origen_mes, x="mes", y="leads", color="origen", barmode="stack",
                 title=f"{linea} — leads por origen y mes ({anio})",
                 labels={"leads": "Leads", "mes": "Mes"})
    st.plotly_chart(fig, use_container_width=True)


def chart_rentabilidad_contable(linea: str):
    st.markdown("#### 📈 Rentabilidad mensual (base imponible − analítica − costo fijo)")
    st.caption(
        "Facturado (base imponible) − costos de `account.analytic.line` en la cuenta de la línea "
        f"({LINEA_ANALYTIC[linea]}) − costo fijo de `data/costos_fijos.csv`."
    )
    if err_costo:
        st.warning(err_costo)
    rent = rentabilidad_contable(linea)
    fig = px.bar(rent, x="mes", y="rentabilidad", text_auto=".2s",
                 title=f"{linea} — rentabilidad contable ({anio})",
                 labels={"rentabilidad": "COP", "mes": "Mes"})
    st.plotly_chart(fig, use_container_width=True)
    with st.expander("Detalle de rentabilidad mensual"):
        st.dataframe(
            rent, use_container_width=True, hide_index=True,
            column_config={
                "mes": "Mes",
                "facturado": st.column_config.NumberColumn("Facturado", format="$%,.0f"),
                "costo": st.column_config.NumberColumn("Costo (analítico + fijo)", format="$%,.0f"),
                "rentabilidad": st.column_config.NumberColumn("Rentabilidad", format="$%,.0f"),
            },
        )


def chart_vendidos_y_proyeccion(linea: str, label: str, col_name: str):
    """Histórico = oportunidades ganadas. Proyección = pipeline con date_deadline (CRM)."""
    st.markdown(f"#### 📈 {label} vendidos (últimos 12 meses) y proyección (próximos 6)")
    st.caption(
        "Histórico = oportunidades GANADAS (`crm.lead.won_status`). "
        "Proyección = oportunidades ABIERTAS con fecha de cierre esperada (`date_deadline`) "
        "en los próximos 6 meses — no es un promedio móvil."
    )
    hist = filtro_linea(won_12m, linea)
    if hist.empty:
        st.info(f"No hay {label.lower()} (oportunidades ganadas) en los últimos 12 meses.")
    else:
        mensual = hist.groupby("mes", as_index=False).agg(**{col_name: ("name", "count")})
        mensual["tipo"] = "Histórico (ganadas)"
    pipe = filtro_linea(pipeline, linea)
    pipe_ok = pipe[pipe["date_deadline"].notna()] if not pipe.empty else pipe
    pipe_ok = pipe_ok[pipe_ok["mes"].isin(meses_6fwd)] if not pipe_ok.empty else pipe_ok
    if pipe_ok is not None and not pipe_ok.empty:
        proy = pipe_ok.groupby("mes", as_index=False).agg(**{col_name: ("name", "count")})
        proy["tipo"] = "Proyección (pipeline CRM)"
    else:
        proy = pd.DataFrame(columns=["mes", col_name, "tipo"])

    partes = []
    if not hist.empty:
        partes.append(mensual)
    if not proy.empty:
        partes.append(proy)
    if not partes:
        st.info("Sin histórico ni pipeline con fecha de cierre para proyectar.")
        return
    combinado = pd.concat(partes, ignore_index=True)
    fig = px.bar(combinado, x="mes", y=col_name, color="tipo",
                 title=f"{label} por mes — histórico y proyección",
                 labels={col_name: label, "mes": "Mes"})
    st.plotly_chart(fig, use_container_width=True)
    if pipe.empty or pipe["date_deadline"].isna().all():
        st.warning("El pipeline abierto no tiene `date_deadline`. Paula: completar la fecha de cierre esperada para que la proyección deje de estar vacía.")
    elif not pipe.empty:
        sin_fecha = int(pipe["date_deadline"].isna().sum())
        if sin_fecha:
            st.caption(f"{sin_fecha} oportunidades abiertas de {linea} no tienen fecha de cierre — no entran a la proyección.")


def render_linea_comun(linea: str, extra_kpi_label: str, extra_kpi_value):
    k = kpis_linea(linea)
    c0, c1, c2, c3, c4 = st.columns(5)
    c0.metric(extra_kpi_label, extra_kpi_value)
    c1.metric("Cumplimiento meta anual", f"{k['pct_cumpl']:.1f}%")
    c2.metric(f"Vendido s/imp. en {linea} (año)", fmt_money(k["vendido_anual"]))
    c3.metric("Facturación s/imp. (año)", fmt_money(k["facturado_anual"]))
    c4.metric("Leads del mes", k["leads_mes"])
    st.caption(
        f"Todos los pesos son **antes de impuestos**. "
        f"**Vendido** = OV confirmadas por **Fecha del pedido**, importe s/imp. "
        f"**convertido a COP** con la TRM (`currency_rate`). "
        f"Si la OV está en USD y no hay tasa, el monto no se convierte (ver aviso arriba). "
        f"**Facturado** = base imponible en COP (`amount_untaxed_signed`; NC restan)."
    )
    chart_venta_vs_meta(linea, k["sales"])
    chart_fact_y_leads(linea, k["invoices"], k["leads"])
    chart_leads_origen(linea, k["leads"])
    with st.expander(f"Detalle OV {linea} (s/imp. en COP)"):
        s = k["sales"]
        if s is None or s.empty:
            st.caption("Sin órdenes confirmadas.")
        else:
            cols = [c for c in [
                "name", "date_order", "cliente", "vendedor", "equipo", "moneda",
                "amount_untaxed", "currency_rate", "amount_untaxed_company", "fx_sin_trm",
            ] if c in s.columns]
            st.dataframe(
                s[cols].sort_values("date_order"),
                use_container_width=True, hide_index=True,
                column_config={
                    "amount_untaxed": st.column_config.NumberColumn("S/imp. moneda OV", format="%,.2f"),
                    "currency_rate": st.column_config.NumberColumn("TRM (currency_rate)", format="%.6f"),
                    "amount_untaxed_company": st.column_config.NumberColumn("S/imp. COP", format="$%,.0f"),
                    "fx_sin_trm": "Sin TRM",
                },
            )
    return k


# ─────────────────────────────────────────────
# Pestañas
# ─────────────────────────────────────────────
tab_resumen, tab_staff, tab_formacion, tab_fabrica, tab_equipo, tab_vendedor = st.tabs(
    ["🏠 Resumen", "👥 Staff", "🎓 Formación", "💻 Fábrica de Software", "🕐 Equipo", "🏆 Vendedores"]
)

# --- Resumen: responde las 6 preguntas --------------------------------
with tab_resumen:
    st.markdown("### Las preguntas del informe")
    st.caption("Respuestas calculadas con Odoo en vivo + metas/costos fijos del CSV.")

    resumen_rows = []
    rent_ytd = {}
    for linea in LINEA_TEAM:
        k = kpis_linea(linea)
        rent = rentabilidad_contable(linea)
        # Para Staff, el neto operativo de plazas es más fiel que el contable
        if linea == "Staff" and staff_req is not None and not staff_req.empty:
            pnl = staffing_pnl_monthly(staff_req, months_year, costo_fijo_de(costos_fijos, "Staff"))
            neto = float(pnl["neto"].sum())
        else:
            neto = float(rent["rentabilidad"].sum())
        rent_ytd[linea] = neto
        resumen_rows.append({
            "linea": linea,
            "meta_anual": k["meta_anual"],
            "vendido": k["vendido_anual"],
            "facturado": k["facturado_anual"],
            "pct_cumpl": k["pct_cumpl"],
            "leads_mes": k["leads_mes"],
            "neto_ytd": neto,
        })
    resumen_df = pd.DataFrame(resumen_rows)

    # 1. ¿Cumplimos metas?
    st.markdown("#### 1. ¿Estamos cumpliendo las metas de ventas anuales?")
    cols = st.columns(3)
    for i, row in resumen_df.iterrows():
        with cols[i]:
            st.metric(
                f"{semaforo(row['pct_cumpl'])} {row['linea']}",
                f"{row['pct_cumpl']:.1f}%",
                help=f"Vendido {fmt_money(row['vendido'])} de meta {fmt_money(row['meta_anual'])}",
            )
            st.caption(f"{fmt_money(row['vendido'])} / {fmt_money(row['meta_anual'])}")
    st.dataframe(
        resumen_df[["linea", "meta_anual", "vendido", "facturado", "pct_cumpl"]],
        use_container_width=True, hide_index=True,
        column_config={
            "linea": "Línea",
            "meta_anual": st.column_config.NumberColumn("Meta año", format="$%,.0f"),
            "vendido": st.column_config.NumberColumn("Vendido s/imp.", format="$%,.0f"),
            "facturado": st.column_config.NumberColumn("Facturado s/imp.", format="$%,.0f"),
            "pct_cumpl": st.column_config.ProgressColumn("% Cumplimiento", format="%.1f%%", min_value=0, max_value=150),
        },
    )
    st.caption(
        f"Montos **antes de impuestos**. Para cuadrar en Odoo · "
        f"**Vendido:** Ventas → Pedidos · Fecha del pedido = {anio} · Confirmado · "
        f"Importe sin impuestos. "
        f"**Facturado:** Facturas de cliente · Fecha de factura = {anio} · Publicadas · "
        f"**Base imponible**. TRANSFORMACION DIGITAL no entra en este tablero."
    )

    # 2. Plazas
    st.markdown("#### 2. ¿Cuántas plazas activas tenemos y qué esperamos en los próximos meses?")
    plazas_hoy = 0
    fuente_plazas = "suscripciones"
    if staff_req is not None and not staff_req.empty:
        plazas_hoy = int((staff_req["state"] == "confirmed").sum())
        fuente_plazas = "firefly.staffing.request (confirmadas)"
        hist_plazas = staffing_coverage(staff_req, meses_12)
        proy_plazas = staffing_coverage(staff_req, meses_6fwd, states=("confirmed",))
    elif subs_df is not None and not subs_df.empty:
        plazas_hoy = int(subs_df["subscription_state"].isin(["3_progress", "4_paused"]).sum())
        fuente_plazas = "sale.order suscripciones en progreso"
        hist_plazas = subscription_coverage(subs_df, meses_12)
        proy_plazas = subscription_coverage(subs_df, meses_6fwd)
    else:
        hist_plazas = pd.DataFrame(columns=["mes", "activas"])
        proy_plazas = pd.DataFrame(columns=["mes", "activas"])

    c1, c2, c3 = st.columns(3)
    c1.metric("Plazas activas hoy", plazas_hoy)
    if not proy_plazas.empty:
        c2.metric("Plazas proyectadas a 6 meses", int(proy_plazas["activas"].iloc[-1]))
        delta = int(proy_plazas["activas"].iloc[-1] - plazas_hoy)
        c3.metric("Variación esperada", f"{delta:+d}")
    st.caption(f"Fuente: {fuente_plazas}. La proyección solo cuenta plazas YA vendidas con vigencia futura (no ventas nuevas).")
    if err_staff:
        st.warning(err_staff)
    if not hist_plazas.empty:
        combo = pd.concat([
            hist_plazas.assign(tipo="Histórico 12 meses"),
            proy_plazas.assign(tipo="Proyección 6 meses"),
        ], ignore_index=True)
        fig = px.line(combo, x="mes", y="activas", color="tipo", markers=True,
                      title="Plazas activas — histórico y proyección",
                      labels={"activas": "Plazas", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)

    # 3. Rentabilidad
    st.markdown("#### 3. ¿Las líneas son rentables?")
    rcols = st.columns(3)
    for i, linea in enumerate(LINEA_TEAM):
        neto = rent_ytd[linea]
        with rcols[i]:
            st.metric(
                f"{'🟢' if neto >= 0 else '🔴'} Neto YTD · {linea}",
                fmt_money(neto),
            )
    st.caption(
        "Staff: ingreso mensual de plazas − costo del recurso (proveedor) − fijo de Diego "
        "(módulo `firefly_staffing`). Formación y Fábrica: facturado − analítica − fijo. "
        "El costo de Diego/Paula está en 0 hasta que Raquel confirme el valor."
    )

    # 4 y 5. Origen y leads
    st.markdown("#### 4 y 5. ¿De dónde vienen las oportunidades? ¿Llegan leads a cada línea?")
    col1, col2 = st.columns(2)
    with col1:
        if leads_all.empty:
            st.info("Sin leads clasificados. Paula: asignar equipo CRM (Staffing IT / FORMACION / FABRICA SOFTWARE).")
        else:
            origen = leads_all.groupby(["linea", "origen"], as_index=False).agg(leads=("name", "count"))
            fig = px.bar(origen, x="origen", y="leads", color="linea", barmode="group",
                         title="Leads del año por origen y línea",
                         labels={"leads": "Leads", "origen": "Origen"})
            st.plotly_chart(fig, use_container_width=True)
            top = (leads_all.groupby("origen", as_index=False).agg(leads=("name", "count"))
                   .sort_values("leads", ascending=False).head(5))
            st.caption("Afianzar los orígenes con más volumen: " + ", ".join(top["origen"].tolist()))
    with col2:
        if leads_all.empty:
            st.info("Sin leads del mes.")
        else:
            leads_mes_l = (leads_all[leads_all["mes"] == mes_actual_key]
                           .groupby("linea", as_index=False).agg(leads=("name", "count")))
            fig = px.bar(leads_mes_l, x="linea", y="leads", text_auto=True,
                         title="Leads del mes actual por línea",
                         labels={"leads": "Leads", "linea": "Línea"})
            st.plotly_chart(fig, use_container_width=True)
            if not won_all.empty:
                abiertas = (leads_all[leads_all["estado"] == "Abierta"]
                            .groupby("linea", as_index=False)
                            .agg(pipeline=("expected_revenue", "sum")))
                fig = px.bar(abiertas, x="linea", y="pipeline", text_auto=".2s",
                             title="Pipeline abierto por línea",
                             labels={"pipeline": "COP", "linea": "Línea"})
                st.plotly_chart(fig, use_container_width=True)

    # 6. Tiempo vs resultado
    st.markdown("#### 6. ¿El equipo dedica tiempo a cada línea y se ven resultados?")
    horas_df, err_horas_res = load_analytic_hours(d1, d2)
    if err_horas_res:
        st.warning(err_horas_res)
    elif horas_df.empty:
        st.info("No hay partes de horas en las cuentas analíticas de las 3 líneas.")
    else:
        horas_l = horas_df.groupby("linea", as_index=False)["unit_amount"].sum()
        mix = resumen_df.merge(horas_l, on="linea", how="left")
        mix["unit_amount"] = mix["unit_amount"].fillna(0)
        mix["cop_por_hora"] = mix.apply(
            lambda r: r["vendido"] / r["unit_amount"] if r["unit_amount"] else 0, axis=1
        )
        fig = px.bar(mix, x="linea", y="unit_amount", text_auto=".1f",
                     title="Horas del año por línea (timesheet analítico)",
                     labels={"unit_amount": "Horas", "linea": "Línea"})
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(
            mix[["linea", "unit_amount", "vendido", "cop_por_hora", "pct_cumpl"]],
            use_container_width=True, hide_index=True,
            column_config={
                "linea": "Línea",
                "unit_amount": st.column_config.NumberColumn("Horas", format="%.1f"),
                "vendido": st.column_config.NumberColumn("Vendido s/imp.", format="$%,.0f"),
                "cop_por_hora": st.column_config.NumberColumn("COP s/imp. / hora", format="$%,.0f"),
                "pct_cumpl": st.column_config.NumberColumn("% meta", format="%.1f%%"),
            },
        )

    col_a, col_b = st.columns(2)
    with col_a:
        if not invoices_all.empty and FACT_COL in invoices_all.columns:
            mensual = invoices_all[invoices_all["linea"] != "Sin línea"].groupby(
                ["mes", "linea"], as_index=False)[FACT_COL].sum()
            fig = px.bar(mensual, x="mes", y=FACT_COL, color="linea", barmode="group",
                         title="Facturación s/imp. mes a mes por línea",
                         labels={FACT_COL: "COP s/imp.", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
    with col_b:
        scol = sales_amount_col(sales_all)
        if not sales_all.empty and scol in sales_all.columns:
            mensual_v = sales_all[sales_all["linea"] != "Sin línea"].groupby(
                ["mes", "linea"], as_index=False)[scol].sum()
            fig = px.bar(mensual_v, x="mes", y=scol, color="linea", barmode="group",
                         title="Vendido s/imp. (OV) mes a mes por línea",
                         labels={scol: "COP s/imp.", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)


# --- Staff --------------------------------------------------------------
with tab_staff:
    if staff_req is not None and not staff_req.empty:
        plazas_actuales = int((staff_req["state"] == "confirmed").sum())
    elif subs_df is not None and not subs_df.empty:
        plazas_actuales = int(subs_df["subscription_state"].isin(["3_progress", "4_paused"]).sum())
    else:
        plazas_actuales = 0

    renov_mes = int(renewals.loc[renewals["mes"] == mes_actual_key].shape[0]) if renewals is not None and not renewals.empty else 0
    render_linea_comun("Staff", "Plazas activas actualmente", plazas_actuales)
    st.metric("Suscripciones renovadas este mes", renov_mes)

    st.divider()
    st.markdown("#### 📈 Tendencia de plazas (12 meses) y proyección (6 meses, plazas ya vendidas)")
    if staff_req is not None and not staff_req.empty:
        historico = staffing_coverage(staff_req, meses_12)
        proyeccion = staffing_coverage(staff_req, meses_6fwd, states=("confirmed",))
        fuente = "vigencia `date_start`/`date_end` de firefly.staffing.request"
    elif subs_df is not None and not subs_df.empty:
        historico = subscription_coverage(subs_df, meses_12)
        proyeccion = subscription_coverage(subs_df, meses_6fwd)
        fuente = "start_date/end_date de suscripciones (sale.order)"
    else:
        historico = pd.DataFrame()
        proyeccion = pd.DataFrame()
        fuente = ""
    if historico.empty:
        st.info("Sin solicitudes Staff ni suscripciones para graficar plazas.")
        if err_staff:
            st.warning(err_staff)
        if err_subs:
            st.warning(err_subs)
    else:
        combinado = pd.concat([
            historico.assign(tipo="Histórico"),
            proyeccion.assign(tipo="Proyección (plazas ya vendidas)"),
        ], ignore_index=True)
        fig = px.line(combinado, x="mes", y="activas", color="tipo", markers=True,
                      title="Plazas activas — histórico y proyección",
                      labels={"activas": "Plazas", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        st.caption(f"Fuente: {fuente}. No incluye ventas futuras aún no cerradas.")

    st.markdown("#### 🔁 Renovaciones por mes")
    if err_ren:
        st.warning(err_ren)
    if renewals is not None and not renewals.empty:
        ren_mes = renewals.groupby("mes", as_index=False).agg(renovaciones=("staff", "count"))
        fig = px.bar(ren_mes, x="mes", y="renovaciones", text_auto=True,
                     title="Renovaciones Staff (firefly.staffing.history)",
                     labels={"renovaciones": "Renovaciones", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
    elif sub_logs is not None and not sub_logs.empty:
        # Transferencia positiva en sale.order.log = renovación confirmada
        transfers = sub_logs[(sub_logs["event_type"] == "3_transfer") & (sub_logs["amount_signed"] > 0)]
        if transfers.empty:
            st.info("No hay renovaciones registradas este año.")
        else:
            t_mes = transfers.groupby("mes", as_index=False).agg(renovaciones=("suscripcion", "count"))
            fig = px.bar(t_mes, x="mes", y="renovaciones", text_auto=True,
                         title="Renovaciones (sale.order.log, transferencias)",
                         labels={"renovaciones": "Renovaciones", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Sin historial de renovaciones. Paula: revisar que las suscripciones se estén renovando en Odoo.")

    st.markdown("#### 💵 Rentabilidad operativa de Staff (plaza vs. recurso)")
    st.caption(
        "Por cada mes de vigencia: valor mensual a cobrar − valor mensual a pagar al proveedor "
        "− costo fijo de Diego. Es el 'cuánto vale el recurso' que pidió el informe."
    )
    if staff_req is not None and not staff_req.empty:
        pnl = staffing_pnl_monthly(staff_req, months_year, costo_fijo_de(costos_fijos, "Staff"))
        fig = px.bar(pnl, x="mes", y="neto", text_auto=".2s",
                     title="Staff — neto operativo mensual (plazas − recurso − Diego)",
                     labels={"neto": "COP", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        col_x, col_y = st.columns(2)
        with col_x:
            fig = px.bar(pnl, x="mes", y=["ingreso_plazas", "costo_recurso"], barmode="group",
                         title="Ingreso de plazas vs. costo del recurso",
                         labels={"value": "COP", "mes": "Mes", "variable": ""})
            st.plotly_chart(fig, use_container_width=True)
        with col_y:
            fig = px.line(pnl, x="mes", y="valor_recurso_promedio", markers=True,
                          title="Costo promedio del recurso / plaza",
                          labels={"valor_recurso_promedio": "COP", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
        with st.expander("Detalle P&L operativo Staff"):
            st.dataframe(
                pnl, use_container_width=True, hide_index=True,
                column_config={
                    "mes": "Mes", "plazas": "Plazas",
                    "ingreso_plazas": st.column_config.NumberColumn("Ingreso plazas", format="$%,.0f"),
                    "costo_recurso": st.column_config.NumberColumn("Costo recurso", format="$%,.0f"),
                    "costo_fijo": st.column_config.NumberColumn("Costo fijo Diego", format="$%,.0f"),
                    "neto": st.column_config.NumberColumn("Neto", format="$%,.0f"),
                    "valor_recurso_promedio": st.column_config.NumberColumn("Recurso promedio", format="$%,.0f"),
                },
            )
        with st.expander("Plazas / solicitudes Staff"):
            show_cols = [c for c in [
                "name", "cliente", "rol", "recurso", "state", "date_start", "date_end",
                "monthly_amount_company_currency", "purchase_amount_company_currency",
                "margin_company_currency",
            ] if c in staff_req.columns]
            st.dataframe(
                staff_req[show_cols], use_container_width=True, hide_index=True,
                column_config={
                    "monthly_amount_company_currency": st.column_config.NumberColumn("Venta mes", format="$%,.0f"),
                    "purchase_amount_company_currency": st.column_config.NumberColumn("Costo recurso", format="$%,.0f"),
                    "margin_company_currency": st.column_config.NumberColumn("Margen", format="$%,.0f"),
                },
            )
    else:
        st.info("Sin `firefly.staffing.request`. Se muestra solo la rentabilidad contable (abajo).")

    chart_rentabilidad_contable("Staff")

    if subs_df is not None and not subs_df.empty:
        with st.expander("Suscripciones de Staff"):
            cols_sub = [c for c in [
                "name", "cliente", "subscription_state", "start_date", "end_date",
                "next_invoice_date", "recurring_monthly",
            ] if c in subs_df.columns]
            st.dataframe(subs_df[cols_sub], use_container_width=True, hide_index=True)


# --- Formación ------------------------------------------------------------
with tab_formacion:
    proj_form = filtro_linea(projects, "Formación") if projects is not None else pd.DataFrame()
    won_form = filtro_linea(won_all, "Formación")
    if not proj_form.empty:
        cursos_entregados = int(len(proj_form))
        extra_label = "Cursos entregados en el año"
        extra_val = cursos_entregados
        st.caption(
            "Entregados = proyectos `service_line=training` con fecha de fin/inicio en el año. "
            "El campo dedicado de fecha de entrega de capacitaciones aún no existe (JUAN Z)."
        )
    else:
        extra_label = "Cursos vendidos en el año (CRM)"
        extra_val = int(len(won_form))
        if err_proj:
            st.info(err_proj)

    render_linea_comun("Formación", extra_label, extra_val)
    chart_vendidos_y_proyeccion("Formación", "Cursos", "cursos")

    if not proj_form.empty:
        st.markdown("#### 🎓 Cursos / proyectos de Formación (entrega proxy)")
        form_mes = proj_form.groupby("mes", as_index=False).agg(cursos=("name", "count"))
        fig = px.bar(form_mes, x="mes", y="cursos", text_auto=True,
                     title="Proyectos de Formación por mes de entrega (proxy)",
                     labels={"cursos": "Cursos", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        with st.expander("Detalle de proyectos Formación"):
            cols_p = [c for c in ["name", "cliente", "responsable", "fecha_entrega", "metodologia_progress"]
                      if c in proj_form.columns]
            st.dataframe(proj_form[cols_p], use_container_width=True, hide_index=True)

    chart_rentabilidad_contable("Formación")


# --- Fábrica de Software --------------------------------------------------
with tab_fabrica:
    proj_fab = filtro_linea(projects, "Fábrica de Software") if projects is not None else pd.DataFrame()
    won_fab = filtro_linea(won_all, "Fábrica de Software")
    if not proj_fab.empty:
        extra_label = "Proyectos acumulados del año"
        extra_val = int(len(proj_fab))
    else:
        extra_label = "Proyectos vendidos en el año (CRM)"
        extra_val = int(len(won_fab))

    render_linea_comun("Fábrica de Software", extra_label, extra_val)
    chart_vendidos_y_proyeccion("Fábrica de Software", "Proyectos", "proyectos")

    if not proj_fab.empty:
        st.markdown("#### 💻 Proyectos de Fábrica (entrega / fin de proyecto)")
        fab_mes = proj_fab.groupby("mes", as_index=False).agg(proyectos=("name", "count"))
        fig = px.bar(fab_mes, x="mes", y="proyectos", text_auto=True,
                     title="Proyectos de Fábrica por mes",
                     labels={"proyectos": "Proyectos", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        with st.expander("Detalle de proyectos Fábrica"):
            cols_p = [c for c in ["name", "cliente", "responsable", "fecha_entrega", "metodologia_progress"]
                      if c in proj_fab.columns]
            st.dataframe(proj_fab[cols_p], use_container_width=True, hide_index=True)

    chart_rentabilidad_contable("Fábrica de Software")


# --- Equipo ---------------------------------------------------------------
with tab_equipo:
    st.markdown("#### ⏱️ Horas del equipo por persona, mes y línea")
    horas_df, err_horas = load_analytic_hours(d1, d2)
    if err_horas:
        st.warning(err_horas)
    elif horas_df.empty:
        st.info("No hay horas registradas (account.analytic.line) en el año.")
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
        with st.expander("Horas por persona, mes y línea"):
            pivot_horas = horas_df.pivot_table(
                index="persona", columns=["linea", "mes"],
                values="unit_amount", aggfunc="sum", fill_value=0,
            )
            st.dataframe(pivot_horas, use_container_width=True)

    st.divider()
    st.markdown("#### ✅ Actividades del equipo por línea y mes (histórico real)")
    st.caption(
        "Fuente: `crm.activity.report` (mensajes de chatter con tipo de actividad). "
        "Mide presentación de negocio, propuesta, socialización y seguimiento — "
        "y el resto de tipos que existan en Odoo."
    )
    act_hist, err_act_h = load_activity_report(d1, d2, team_ids)
    if err_act_h:
        st.warning(err_act_h)
    elif act_hist.empty:
        st.info("No hay actividades completadas en `crm.activity.report` este año. Paula: registrar actividades en el CRM.")
    else:
        comercial = act_hist[act_hist["tipo"].map(es_tipo_comercial)]
        usar = comercial if not comercial.empty else act_hist
        if comercial.empty:
            st.caption("No coincidió ningún tipo con presentación/propuesta/socialización/seguimiento. Se muestran todos.")
        col3, col4 = st.columns(2)
        with col3:
            por_linea = usar.groupby(["mes", "linea"], as_index=False).agg(cantidad=("lead_id", "count"))
            fig = px.bar(por_linea, x="mes", y="cantidad", color="linea", barmode="group",
                         title="Actividades completadas por línea y mes",
                         labels={"cantidad": "Actividades", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)
        with col4:
            por_tipo = usar.groupby("tipo", as_index=False).agg(cantidad=("lead_id", "count"))
            fig = px.bar(por_tipo.sort_values("cantidad"), x="cantidad", y="tipo", orientation="h",
                         title="Actividades por tipo", text_auto=True,
                         labels={"cantidad": "Actividades", "tipo": ""})
            st.plotly_chart(fig, use_container_width=True)
        with st.expander("Detalle de actividades completadas"):
            st.dataframe(
                usar[["mes", "tipo", "vendedor", "linea"]].sort_values(["mes", "linea"]),
                use_container_width=True, hide_index=True,
            )

    st.markdown("#### ⏳ Backlog de actividades pendientes (foto de hoy)")
    st.caption("Odoo borra `mail.activity` al completarlas. Esto es solo lo que sigue abierto.")
    actividades, err_act = load_team_activities(team_ids)
    if err_act:
        st.warning(err_act)
    elif actividades.empty:
        st.info("No hay actividades pendientes sobre oportunidades.")
    else:
        por_linea_mes = actividades.groupby(["mes", "equipo"], as_index=False).agg(cantidad=("res_id", "count"))
        fig = px.bar(por_linea_mes, x="mes", y="cantidad", color="equipo", barmode="group",
                     title="Pendientes por línea y mes de vencimiento",
                     labels={"cantidad": "Actividades", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)

    st.divider()
    st.markdown("#### 🕑 Horas dedicadas a cada actividad (reuniones de calendario)")
    st.caption(
        "`mail.activity` no guarda duración. Proxy: `calendar.event.duration` de reuniones "
        "ligadas a una oportunidad del equipo. El nombre de la reunión se usa como tipo "
        "(ej. 'Propuesta X')."
    )
    cal, err_cal = load_calendar_hours(d1, d2, team_ids)
    if err_cal:
        st.warning(err_cal)
    elif cal.empty:
        st.info("No hay reuniones de calendario ligadas a oportunidades de estas líneas.")
    else:
        cal["tipo_corto"] = cal["tipo"].str.slice(0, 40)
        por_persona = (cal.groupby(["persona", "linea", "mes"], as_index=False)["duration"].sum())
        fig = px.bar(por_persona, x="mes", y="duration", color="persona", facet_col="linea",
                     title="Horas de reunión por persona, mes y línea",
                     labels={"duration": "Horas", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        with st.expander("Horas por persona y nombre de reunión"):
            det = (cal.groupby(["persona", "linea", "tipo_corto"], as_index=False)["duration"]
                   .sum().sort_values("duration", ascending=False))
            st.dataframe(det, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("#### 🏗️ Top 5 proyectos por horas — vendedores de las 3 líneas")
    vendedores_df = load_sales_team_employees(team_ids)
    if vendedores_df.empty:
        st.info("No hay miembros en los equipos de venta para cruzar con timesheets.")
    else:
        horas_proy_df, err_proy = load_hours_by_project(d1, d2, vendedores_df["employee_id"].tolist())
        if err_proy:
            st.warning(err_proy)
        elif horas_proy_df.empty:
            st.info("No hay horas por proyecto para estos vendedores.")
        else:
            resumen_proy = (horas_proy_df.groupby("proyecto", as_index=False)["unit_amount"].sum()
                            .sort_values("unit_amount", ascending=False))
            total = resumen_proy["unit_amount"].sum()
            resumen_proy["pct"] = resumen_proy["unit_amount"] / total * 100 if total else 0
            top5 = resumen_proy.head(5)["proyecto"].tolist()
            st.dataframe(
                resumen_proy, use_container_width=True, hide_index=True,
                column_config={
                    "proyecto": "Proyecto",
                    "unit_amount": st.column_config.NumberColumn("Horas", format="%.2f"),
                    "pct": st.column_config.NumberColumn("%", format="%.2f%%"),
                },
            )
            top5_df = horas_proy_df[horas_proy_df["proyecto"].isin(top5)]
            mensual_top5 = top5_df.groupby(["mes", "proyecto"], as_index=False)["unit_amount"].sum()
            fig = px.line(mensual_top5, x="mes", y="unit_amount", color="proyecto", markers=True,
                          title="Top 5 proyectos — horas por mes",
                          labels={"unit_amount": "Horas", "mes": "Mes"})
            st.plotly_chart(fig, use_container_width=True)


# --- Vendedores -----------------------------------------------------------
with tab_vendedor:
    st.caption("Cierres (CRM ganadas) y facturación por vendedor, mes y línea.")
    won_vend = won_all[won_all["linea"] != "Sin línea"] if not won_all.empty else won_all
    fact_vend = invoices_all[invoices_all["linea"] != "Sin línea"] if not invoices_all.empty else invoices_all
    sales_vend = sales_all[sales_all["linea"] != "Sin línea"] if not sales_all.empty else sales_all

    st.markdown("#### 🏆 Cierres (nuevos negocios) por vendedor, mes y línea")
    if won_vend.empty:
        st.info("No hay oportunidades ganadas en el período.")
    else:
        cierres = won_vend.groupby(["mes", "vendedor", "linea"], as_index=False).agg(cierres=("name", "count"))
        fig = px.bar(cierres, x="mes", y="cierres", color="vendedor", barmode="group", facet_col="linea",
                     title="Cierres por vendedor, mes y línea",
                     labels={"cierres": "Cierres", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        with st.expander("Detalle de cierres"):
            st.dataframe(cierres.sort_values(["linea", "mes"]), use_container_width=True, hide_index=True)

    st.markdown("#### 🧾 Facturación s/imp. por vendedor, mes y línea")
    if fact_vend.empty or FACT_COL not in fact_vend.columns:
        st.info("No hay facturas en el período.")
    else:
        fact_v = fact_vend.groupby(["mes", "vendedor", "linea"], as_index=False)[FACT_COL].sum()
        fig = px.bar(fact_v, x="mes", y=FACT_COL, color="vendedor", barmode="group", facet_col="linea",
                     title="Facturación s/imp. por vendedor, mes y línea",
                     labels={FACT_COL: "COP s/imp.", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)
        with st.expander("Detalle de facturación"):
            st.dataframe(
                fact_v.sort_values(["linea", "mes"]), use_container_width=True, hide_index=True,
                column_config={FACT_COL: st.column_config.NumberColumn("Facturado s/imp.", format="$%,.0f")},
            )

    st.markdown("#### 📦 Órdenes confirmadas por vendedor, mes y línea (s/imp.)")
    scol = sales_amount_col(sales_vend)
    if sales_vend.empty or scol not in sales_vend.columns:
        st.info("No hay órdenes confirmadas en el período.")
    else:
        ov = sales_vend.groupby(["mes", "vendedor", "linea"], as_index=False).agg(
            ordenes=("name", "count"), vendido=(scol, "sum")
        )
        fig = px.bar(ov, x="mes", y="ordenes", color="vendedor", barmode="group", facet_col="linea",
                     title="Nº de OV confirmadas por vendedor",
                     labels={"ordenes": "Órdenes", "mes": "Mes"})
        st.plotly_chart(fig, use_container_width=True)


with st.sidebar.expander("Fuentes Odoo y pendientes"):
    st.markdown(
        """
- **Plazas** → `firefly.staffing.request` (fallback: suscripciones)
- **Renovaciones** → `firefly.staffing.history` (fallback: `sale.order.log`)
- **Vendido** → OV confirmadas por `date_order`, s/imp. en COP (`amount_untaxed` ÷ `currency_rate`)
- **Facturas** → publicadas por `invoice_date`, `amount_untaxed_signed` (ya en COP; NC restan)
- **Leads / origen** → `crm.lead` + `source_id` (equipo CRM)
- **Cursos/proyectos entregados** → `project.project.service_line` (fecha fin = proxy)
- **Actividades hechas** → `crm.activity.report`
- **Horas por actividad** → `calendar.event.duration` (proxy)
- **Pendiente JUAN Z:** campo de fecha de entrega de capacitaciones
- **Pendiente PAULA / Raquel:** costo fijo Diego y Paula ≠ 0; equipos en facturas/OV Staff; `date_deadline` en pipeline
        """
    )
