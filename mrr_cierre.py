# -*- coding: utf-8 -*-
"""Cierre Staff alineado a sale.order.log.report (MRR Breakdown de Odoo).

Módulo aparte para que Streamlit Cloud no dependa de exports nuevos en odoo_io
(había race/cache dejando odoo_io viejo con streamlit_app nuevo).
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
    subscription_cierre_monthly as _subscription_cierre_monthly_odoo,
)


def plan_period_months(value, unit) -> float:
    """Meses equivalentes del período de facturación del plan."""
    try:
        v = float(value) if value is not None and not pd.isna(value) else 1.0
    except (TypeError, ValueError):
        v = 1.0
    unit = (unit or "month").lower()
    if unit == "year":
        return v * 12.0
    if unit == "week":
        return v * 7.0 / 30.437
    if unit == "day":
        return v / 30.437
    return max(v, 1.0)


@st.cache_data(ttl=600, show_spinner="Cargando MRR Breakdown (sale.order.log.report)...")
def load_sale_order_log_report(date_from: str, date_to: str, team_ids: list[int]):
    """Fuente de cierre: sale.order.log.report (fallback sale.order.log)."""
    cols = [
        "event_type", "event_date", "first_contract_date", "order_id", "origin_order_id",
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
    domain = [
        ("event_date", ">=", date_from),
        ("event_date", "<=", date_to),
        ("event_type", "=", "0_creation"),
    ]
    if team_ids and "team_id" in have:
        domain.append(("team_id", "in", team_ids))
    try:
        df = search_read(model, domain, pick_fields(model, cols))
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
    df["plan_id_num"] = m2o_id(df["plan_id"]) if "plan_id" in df else None
    df["_model"] = model
    return df, None


@st.cache_data(ttl=600, show_spinner="Cargando planes de suscripción...")
def load_subscription_plans() -> pd.DataFrame:
    if "sale.subscription.plan" not in _models_exist(("sale.subscription.plan",)):
        return pd.DataFrame(columns=["id", "name", "billing_period_value", "billing_period_unit"])
    cols = ["name", "billing_period_value", "billing_period_unit"]
    try:
        df = search_read("sale.subscription.plan", [], pick_fields("sale.subscription.plan", cols))
    except Exception:
        return pd.DataFrame(columns=["id", "name", "billing_period_value", "billing_period_unit"])
    if df.empty:
        return df
    df["billing_period_value"] = pd.to_numeric(df.get("billing_period_value", 1), errors="coerce").fillna(1.0)
    df["billing_period_unit"] = df.get("billing_period_unit", "month").fillna("month").astype(str)
    return df


def _plan_factor_series(plan_ids: pd.Series, plans: pd.DataFrame | None) -> pd.Series:
    factor = pd.Series(1.0, index=plan_ids.index)
    if plans is None or plans.empty or "id" not in plans.columns:
        return factor
    plan_map = plans.set_index("id")
    for idx, pid in plan_ids.items():
        if pd.isna(pid):
            continue
        try:
            pid_i = int(pid)
        except (TypeError, ValueError):
            continue
        if pid_i not in plan_map.index:
            continue
        row = plan_map.loc[pid_i]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        factor.loc[idx] = plan_period_months(
            row.get("billing_period_value", 1),
            row.get("billing_period_unit", "month"),
        )
    return factor


def _valor_desde_mrr_y_plan(amount: pd.Series, mrr: pd.Series, factor: pd.Series) -> list[float]:
    """amount_signed (MRR) × período del plan; no remultiplica si ya es total del período."""
    valores = []
    for a, m, f in zip(amount, mrr, factor):
        f = float(f) if f else 1.0
        if f <= 1.01:
            valores.append(float(a))
            continue
        periodo = (m if m else a) * f
        if m > 0 and abs(a - m) / max(abs(m), 1.0) < 0.08:
            valores.append(float(a) * f)
        elif abs(a - periodo) / max(abs(periodo), 1.0) < 0.08:
            valores.append(float(a))
        else:
            valores.append(float(a) * f)
    return valores


def log_cierre_monthly(logs: pd.DataFrame, months: list[str],
                       team_id: int | None = None,
                       plans: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cierre desde eventos New: amount_signed × período del plan."""
    model = "sale.order.log.report"
    if logs is not None and not logs.empty and "_model" in logs.columns:
        model = str(logs["_model"].iloc[0])
    fuente = f"{model} · New · amount_signed × período del plan"
    if not months:
        return pd.DataFrame(columns=["mes", "vendido", "fuente"])
    empty = pd.DataFrame({"mes": months, "vendido": [0.0] * len(months),
                          "fuente": [fuente] * len(months)})
    if logs is None or logs.empty:
        return empty
    df = logs.copy()
    if team_id is not None and "equipo_id" in df.columns:
        df = df[pd.to_numeric(df["equipo_id"], errors="coerce") == int(team_id)]
    if "event_type" in df.columns:
        df = df[df["event_type"] == "0_creation"]
    if df.empty:
        return empty

    if "first_contract_date" in df.columns and df["first_contract_date"].notna().any():
        df["_mes_src"] = df["first_contract_date"].fillna(df.get("event_date"))
    else:
        df["_mes_src"] = df["event_date"]
    df = df[df["_mes_src"].notna()].copy()
    if df.empty:
        return empty
    df["mes"] = pd.to_datetime(df["_mes_src"]).dt.to_period("M").astype(str)

    amount = pd.to_numeric(df.get("amount_signed", 0), errors="coerce").fillna(0.0)
    mrr_log = pd.to_numeric(df.get("recurring_monthly", 0), errors="coerce").fillna(0.0)
    plan_ids = df["plan_id_num"] if "plan_id_num" in df.columns else pd.Series(pd.NA, index=df.index)
    factor = _plan_factor_series(plan_ids, plans)
    df["_valor"] = _valor_desde_mrr_y_plan(amount, mrr_log, factor)
    por_mes = df.groupby("mes", as_index=False)["_valor"].sum().rename(columns={"_valor": "vendido"})
    out = pd.DataFrame({"mes": months}).merge(por_mes, on="mes", how="left")
    out["vendido"] = out["vendido"].fillna(0.0)
    out["fuente"] = fuente
    return out


def staff_cierre_monthly(requests: pd.DataFrame | None, subs: pd.DataFrame | None,
                         months: list[str], team_id: int | None = None,
                         logs: pd.DataFrame | None = None,
                         plans: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cierre: log.report New → fallback suscripciones (odoo_io) → staffing."""
    if logs is not None and not logs.empty:
        out = log_cierre_monthly(logs, months, team_id=team_id, plans=plans)
        if out["vendido"].sum() > 0:
            return out
    # Preferir signature nueva de odoo_io si acepta plans=
    try:
        return _subscription_cierre_monthly_odoo(subs if subs is not None else pd.DataFrame(),
                                                 months, team_id=team_id, plans=plans)
    except TypeError:
        pass
    if subs is not None and not subs.empty:
        try:
            return _subscription_cierre_monthly_odoo(
                subs, months, team_id=team_id
            )
        except TypeError:
            return _subscription_cierre_monthly_odoo(subs, months)
    return staffing_cierre_monthly(
        requests if requests is not None else pd.DataFrame(), months
    )


__all__ = [
    "load_sale_order_log_report",
    "load_subscription_plans",
    "plan_period_months",
    "log_cierre_monthly",
    "staff_cierre_monthly",
    "staff_cierre_detail",
]
