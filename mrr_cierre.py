# -*- coding: utf-8 -*-
"""Cierre Staff — valor vendido en la fecha del primer contrato.

Regla de negocio (ventas recurrentes):
  En la fecha del contrato se toma el valor mensual (MRR) y *hasta cuándo va*
  la suscripción. Ese producto es lo vendido.

  Ejemplos:
    - 8.000.000 / mes × 3 meses  →  24.000.000 en el mes del contrato
    - 14.000.000 / mes × 1 mes   →  14.000.000 en el mes del contrato

  Meses = de start_date a end_date (inclusive).
  Sin fecha fin → 1 mes (solo el valor de ese período).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from odoo_io import (
    _models_exist,
    available_fields,
    m2o_id,
    m2o_name,
    pick_fields,
    search_read,
    staff_cierre_detail,
    staffing_cierre_monthly,
)


def contract_months(start, end) -> int:
    """Meses de duración inclusive (start→end). Sin fin → 1."""
    if pd.isna(start):
        return 1
    start_p = pd.Period(start, freq="M")
    if pd.isna(end):
        return 1
    end_p = pd.Period(end, freq="M")
    if end_p < start_p:
        return 1
    return int((end_p - start_p).n) + 1


def valor_vendido_contrato(mrr, start, end) -> float:
    """MRR × meses hasta el fin del contrato."""
    try:
        m = float(mrr) if mrr is not None and not pd.isna(mrr) else 0.0
    except (TypeError, ValueError):
        m = 0.0
    if m <= 0:
        return 0.0
    return m * contract_months(start, end)


# ── loaders de apoyo (detalle / planes) ──────────────────────────────


@st.cache_data(ttl=600, show_spinner="Cargando MRR Breakdown (sale.order.log.report)...")
def load_sale_order_log_report(date_from: str, date_to: str, team_ids: list[int]):
    """Detalle opcional desde el reporte de Odoo (no define el valor del gráfico)."""
    cols = [
        "event_type", "event_date", "first_contract_date", "order_id",
        "team_id", "plan_id", "amount_signed", "recurring_monthly", "subscription_state",
    ]
    existing = _models_exist(("sale.order.log.report", "sale.order.log"))
    if "sale.order.log.report" in existing:
        model = "sale.order.log.report"
    elif "sale.order.log" in existing:
        model = "sale.order.log"
    else:
        return pd.DataFrame(columns=cols), None

    have = available_fields(model)
    fields = list(pick_fields(model, cols) if have else cols)
    for must in ("event_type", "event_date", "order_id", "amount_signed"):
        if must not in fields:
            fields.append(must)

    domain = [
        ("event_date", ">=", date_from),
        ("event_date", "<=", date_to),
        ("event_type", "=", "0_creation"),
    ]
    if team_ids and (not have or "team_id" in have):
        domain.append(("team_id", "in", team_ids))
    try:
        df = search_read(model, domain, fields)
    except Exception as e:
        return pd.DataFrame(columns=cols), f"No se pudo leer {model}: {e}"
    if df.empty:
        return df, None
    df["event_date"] = pd.to_datetime(df.get("event_date"), errors="coerce")
    if "first_contract_date" in df.columns:
        df["first_contract_date"] = pd.to_datetime(df["first_contract_date"], errors="coerce")
    else:
        df["first_contract_date"] = pd.NaT
    df["suscripcion"] = m2o_name(df["order_id"]) if "order_id" in df else ""
    df["equipo"] = m2o_name(df["team_id"]) if "team_id" in df else "Sin asignar"
    df["equipo_id"] = m2o_id(df["team_id"]) if "team_id" in df else None
    df["plan"] = m2o_name(df["plan_id"]) if "plan_id" in df else ""
    df["_model"] = model
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando planes de suscripción...")
def load_subscription_plans() -> pd.DataFrame:
    """Se mantiene por compatibilidad con imports de la app; el cierre no depende del plan."""
    if "sale.subscription.plan" not in _models_exist(("sale.subscription.plan",)):
        return pd.DataFrame(columns=["id", "name", "billing_period_value", "billing_period_unit"])
    cols = ["name", "billing_period_value", "billing_period_unit"]
    try:
        df = search_read("sale.subscription.plan", [], pick_fields("sale.subscription.plan", cols))
    except Exception:
        return pd.DataFrame(columns=["id", "name", "billing_period_value", "billing_period_unit"])
    if df.empty:
        return df
    if "id" in df.columns:
        df["id"] = pd.to_numeric(df["id"], errors="coerce")
    df["billing_period_value"] = pd.to_numeric(df.get("billing_period_value", 1), errors="coerce").fillna(1.0)
    df["billing_period_unit"] = df.get("billing_period_unit", "month").fillna("month").astype(str)
    return df


def plan_period_months(value, unit) -> float:
    """Compat: ya no se usa para el cierre; se deja por imports legacy."""
    try:
        v = float(value) if value is not None and not pd.isna(value) else 1.0
    except (TypeError, ValueError):
        v = 1.0
    unit = (str(unit or "month")).lower()
    if unit == "year":
        return v * 12.0
    if unit == "week":
        return v * 7.0 / 30.437
    if unit == "day":
        return v / 30.437
    return max(v, 1.0)


# ── cálculo del gráfico ──────────────────────────────────────────────


def subscription_cierre_from_subs(subs: pd.DataFrame, months: list[str],
                                  team_id: int | None = None,
                                  plans: pd.DataFrame | None = None) -> pd.DataFrame:
    """Suma en el mes de first_contract_date: MRR × meses (start→end)."""
    from odoo_io import _subscription_cierre_frame

    fuente = "MRR × meses del contrato (fecha primer contrato → fin)"
    if not months:
        return pd.DataFrame(columns=["mes", "vendido", "fuente"])
    empty = pd.DataFrame({"mes": months, "vendido": [0.0] * len(months),
                          "fuente": [fuente] * len(months)})

    sub = _subscription_cierre_frame(subs, team_id=team_id)
    if sub.empty:
        return empty
    sub = sub.copy()
    sub = sub[sub["first_contract_date"].notna()].copy()
    if sub.empty:
        return empty

    sub["mes"] = sub["first_contract_date"].dt.to_period("M").astype(str)
    sub = sub[sub["mes"].isin(months)].copy()
    if sub.empty:
        return empty

    # Inicio de vigencia: start_date, si falta first_contract_date
    start = sub["start_date"] if "start_date" in sub.columns else sub["first_contract_date"]
    start = start.fillna(sub["first_contract_date"])
    end = sub["end_date"] if "end_date" in sub.columns else pd.Series(pd.NaT, index=sub.index)
    mrr = pd.to_numeric(sub.get("recurring_monthly", 0), errors="coerce").fillna(0.0)

    sub["meses_contrato"] = [contract_months(s, e) for s, e in zip(start, end)]
    sub["valor_vendido"] = [
        valor_vendido_contrato(m, s, e) for m, s, e in zip(mrr, start, end)
    ]

    por_mes = (
        sub.groupby("mes", as_index=False)["valor_vendido"]
        .sum()
        .rename(columns={"valor_vendido": "vendido"})
    )
    out = pd.DataFrame({"mes": months}).merge(por_mes, on="mes", how="left")
    out["vendido"] = out["vendido"].fillna(0.0)
    out["fuente"] = fuente
    return out


def log_cierre_monthly(logs: pd.DataFrame, months: list[str],
                       team_id: int | None = None,
                       plans: pd.DataFrame | None = None,
                       subs: pd.DataFrame | None = None) -> pd.DataFrame:
    """No se usa para el gráfico; se conserva la firma por compatibilidad."""
    fuente = "sale.order.log.report (solo detalle)"
    return pd.DataFrame({"mes": months, "vendido": [0.0] * len(months),
                         "fuente": [fuente] * len(months)})


def staff_cierre_monthly(requests: pd.DataFrame | None, subs: pd.DataFrame | None,
                         months: list[str], team_id: int | None = None,
                         logs: pd.DataFrame | None = None,
                         plans: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cierre comercial = MRR × duración del contrato en el mes del primer contrato."""
    if subs is not None and not subs.empty:
        out = subscription_cierre_from_subs(subs, months, team_id=team_id, plans=plans)
        if float(out["vendido"].sum()) > 0:
            return out
    return staffing_cierre_monthly(
        requests if requests is not None else pd.DataFrame(), months
    )


__all__ = [
    "load_sale_order_log_report",
    "load_subscription_plans",
    "plan_period_months",
    "contract_months",
    "valor_vendido_contrato",
    "log_cierre_monthly",
    "staff_cierre_monthly",
    "staff_cierre_detail",
    "subscription_cierre_from_subs",
]
