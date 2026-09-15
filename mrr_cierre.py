# -*- coding: utf-8 -*-
"""Cierre Staff — valor vendido en el mes de create_date de la OV.

Regla de negocio:
  Valor = `expected_revenue` de la oportunidad CRM (`sale.order.opportunity_id`).
  Mes del gráfico = create_date de la OV.

  Fallback si no hay oportunidad / expected_revenue=0:
    MRR × ciclos mensuales + (MRR/30) × días (aniversarios start→end),
    o amount_untaxed si no hay MRR.

  Ej. oportunidad «4 meses × 10M» con expected_revenue=40M → 40M en create_date.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st
from dateutil.relativedelta import relativedelta

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


def contract_duration(start, end) -> tuple[int, int]:
    """(ciclos_mensuales, días_tras_último_aniversario). Fallback MRR.

    11-ago → 16-oct → (3, 5);  8-sep → 15-dic → (4, 7).
    Sin fin → (1, 0).
    """
    if pd.isna(start):
        return 1, 0
    s = pd.Timestamp(start).normalize()
    if pd.isna(end):
        return 1, 0
    e = pd.Timestamp(end).normalize()
    if e < s:
        return 1, 0
    if e == s:
        return 1, 0

    cycles = 0
    for n in range(0, 600):
        anniversary = s + relativedelta(months=n)
        if anniversary > e:
            break
        cycles = n + 1
    if cycles <= 0:
        return 1, 0

    last = s + relativedelta(months=cycles - 1)
    days_extra = max(int((e - last).days), 0)
    return cycles, days_extra


def contract_months(start, end) -> int:
    """Ciclos mensuales (compat). Sin fin → 1."""
    months, _days = contract_duration(start, end)
    return max(months, 1)


def valor_vendido_contrato(mrr, start, end, amount_untaxed=0.0,
                           expected_revenue=0.0) -> float:
    """Valor vendido en el mes de create_date.

    Preferencia: expected_revenue de la oportunidad → MRR×ciclos+(MRR/30)×días
    → amount_untaxed.
    """
    try:
        opp = float(expected_revenue) if expected_revenue is not None and not pd.isna(expected_revenue) else 0.0
    except (TypeError, ValueError):
        opp = 0.0
    if opp > 0:
        return opp
    try:
        m = float(mrr) if mrr is not None and not pd.isna(mrr) else 0.0
    except (TypeError, ValueError):
        m = 0.0
    try:
        untaxed = float(amount_untaxed) if amount_untaxed is not None and not pd.isna(amount_untaxed) else 0.0
    except (TypeError, ValueError):
        untaxed = 0.0
    if m > 0:
        months, days = contract_duration(start, end)
        return m * months + (m / 30.0) * days
    return max(untaxed, 0.0)


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
    """Suma en el mes de create_date: expected_revenue de la oportunidad."""
    from odoo_io import _subscription_cierre_frame

    fuente = "crm.lead.expected_revenue (oportunidad · mes create_date)"
    if not months:
        return pd.DataFrame(columns=["mes", "vendido", "fuente"])
    empty = pd.DataFrame({"mes": months, "vendido": [0.0] * len(months),
                          "fuente": [fuente] * len(months)})

    sub = _subscription_cierre_frame(subs, team_id=team_id)
    if sub.empty:
        return empty
    sub = sub.copy()
    if "create_date" not in sub.columns:
        sub["create_date"] = pd.NaT
    if "date_order" in sub.columns:
        miss = sub["create_date"].isna()
        sub.loc[miss, "create_date"] = sub.loc[miss, "date_order"]
    sub = sub[sub["create_date"].notna()].copy()
    if sub.empty:
        return empty

    sub["mes"] = sub["create_date"].dt.to_period("M").astype(str)
    sub = sub[sub["mes"].isin(months)].copy()
    if sub.empty:
        return empty

    start = sub["start_date"] if "start_date" in sub.columns else sub["create_date"]
    start = start.fillna(sub["create_date"])
    if "first_contract_date" in sub.columns:
        start = start.fillna(sub["first_contract_date"])
    end = sub["end_date"] if "end_date" in sub.columns else pd.Series(pd.NaT, index=sub.index)
    mrr = pd.to_numeric(sub.get("recurring_monthly", 0), errors="coerce").fillna(0.0)
    untaxed = pd.to_numeric(sub.get("amount_untaxed", 0), errors="coerce").fillna(0.0)
    opp = pd.to_numeric(sub.get("expected_revenue", 0), errors="coerce").fillna(0.0)

    dur = [contract_duration(s, e) for s, e in zip(start, end)]
    sub["meses_contrato"] = [d[0] for d in dur]
    sub["dias_contrato"] = [d[1] for d in dur]
    sub["valor_vendido"] = [
        valor_vendido_contrato(m, s, e, u, o)
        for m, s, e, u, o in zip(mrr, start, end, untaxed, opp)
    ]
    # Marcar fuente mixta si alguna fila usó fallback.
    used_opp = int((opp > 0).sum())
    if used_opp == 0:
        fuente = "fallback MRR×ciclos+(MRR/30)×días (sin expected_revenue)"
    elif used_opp < len(sub):
        fuente = "crm.lead.expected_revenue (+ fallback MRR si falta opp)"

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
    """Cierre comercial = expected_revenue de la oportunidad (mes create_date)."""
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
    "contract_duration",
    "contract_months",
    "valor_vendido_contrato",
    "log_cierre_monthly",
    "staff_cierre_monthly",
    "staff_cierre_detail",
    "subscription_cierre_from_subs",
]
