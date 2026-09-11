# -*- coding: utf-8 -*-
"""Cierre Staff alineado a sale.order.log.report (MRR Breakdown de Odoo).

Valor de cierre = importe de **un período de facturación** del plan:
  - Preferir `sale.order.recurring_total` (lo que Odoo cobra por período).
  - Si no: `amount_signed` (MRR del log) × `billing_period` del plan.
  - Nunca × vigencia calendario start→end.
"""

from __future__ import annotations

import re

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


def plan_period_months(value, unit) -> float:
    """Meses equivalentes del período de facturación del plan."""
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


def _period_months_from_plan_name(name) -> float | None:
    """Fallback si no hay plan_id: '3 Months', 'Trimestral', 'Mensual', etc."""
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return None
    s = str(name).strip().lower()
    if not s or s in ("sin asignar", "false", "none"):
        return None
    if "trimestr" in s or "quarter" in s:
        return 3.0
    if "semestr" in s or "semester" in s or "semi" in s:
        return 6.0
    if "anual" in s or "annual" in s or "yearly" in s or "year" in s:
        return 12.0
    if "mensual" in s or "month" in s or "monthly" in s:
        # "3 months" / "2 months"
        m = re.search(r"(\d+)\s*m", s)
        if m:
            return float(m.group(1))
        return 1.0
    m = re.search(r"(\d+)\s*(mes|month)", s)
    if m:
        return float(m.group(1))
    return None


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
    # No filtrar plan_id con pick_fields si fields_get del SQL view es incompleto:
    # pedimos wanted y dejamos que Odoo ignore lo que no exista.
    fields = pick_fields(model, cols) if have else cols
    for must in ("event_type", "event_date", "order_id", "amount_signed", "team_id", "plan_id"):
        if must not in fields and (not have or must in have or must == "plan_id"):
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
    except Exception:
        # Reintento sin plan_id / first_contract_date (campos que a veces fallan en la view).
        fields_min = [f for f in fields if f not in ("plan_id", "first_contract_date", "origin_order_id")]
        try:
            df = search_read(model, domain, fields_min)
        except Exception as e2:
            return pd.DataFrame(columns=cols), f"No se pudo leer {model}: {e2}"
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
    df["order_id_num"] = m2o_id(df["order_id"]) if "order_id" in df else None
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
    if "id" not in df.columns:
        return pd.DataFrame(columns=["id", "name", "billing_period_value", "billing_period_unit"])
    df["id"] = pd.to_numeric(df["id"], errors="coerce")
    df["billing_period_value"] = pd.to_numeric(df.get("billing_period_value", 1), errors="coerce").fillna(1.0)
    df["billing_period_unit"] = df.get("billing_period_unit", "month").fillna("month").astype(str)
    return df


def _enrich_logs_from_subs(df: pd.DataFrame, subs: pd.DataFrame | None) -> pd.DataFrame:
    """Completa plan_id / recurring_total / amount_untaxed desde sale.order."""
    if subs is None or subs.empty or "order_id_num" not in df.columns:
        return df
    sub = subs.copy()
    if "id" not in sub.columns:
        return df
    sub["id"] = pd.to_numeric(sub["id"], errors="coerce")
    sub = sub[sub["id"].notna()].drop_duplicates(subset=["id"], keep="first")
    sub = sub.set_index("id")

    order_ids = pd.to_numeric(df["order_id_num"], errors="coerce")

    def _lookup(oid, col, default=None):
        if pd.isna(oid) or oid not in sub.index:
            return default
        row = sub.loc[oid]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        return row.get(col, default)

    if "recurring_total" not in df.columns:
        df["recurring_total"] = [ _lookup(oid, "recurring_total", 0) for oid in order_ids ]
    if "amount_untaxed" not in df.columns:
        df["amount_untaxed"] = [ _lookup(oid, "amount_untaxed", 0) for oid in order_ids ]

    # plan_id del pedido si el log no lo trae
    plan_from_so = []
    plan_name_from_so = []
    for oid in order_ids:
        if pd.isna(oid) or oid not in sub.index:
            plan_from_so.append(None)
            plan_name_from_so.append("")
            continue
        row = sub.loc[oid]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        pid = row.get("plan_id")
        # plan_id puede ser m2o [id, name] o ya numérico
        if isinstance(pid, (list, tuple)) and pid:
            try:
                plan_from_so.append(int(pid[0]))
            except (TypeError, ValueError):
                plan_from_so.append(None)
            plan_name_from_so.append(str(pid[1]) if len(pid) > 1 else "")
        else:
            try:
                plan_from_so.append(int(pid) if pd.notna(pid) else None)
            except (TypeError, ValueError):
                plan_from_so.append(None)
            plan_name_from_so.append(str(row.get("plan", "") or ""))

    if "plan_id_num" not in df.columns:
        df["plan_id_num"] = plan_from_so
    else:
        df["plan_id_num"] = pd.to_numeric(df["plan_id_num"], errors="coerce")
        missing = df["plan_id_num"].isna()
        df.loc[missing, "plan_id_num"] = pd.Series(plan_from_so, index=df.index)[missing]

    if "plan" not in df.columns or df["plan"].fillna("").eq("").all() or (df["plan"] == "Sin asignar").all():
        df["plan"] = plan_name_from_so
    else:
        blank = df["plan"].isna() | df["plan"].astype(str).isin(["", "Sin asignar", "False"])
        df.loc[blank, "plan"] = pd.Series(plan_name_from_so, index=df.index)[blank]

    return df


def _plan_factor_series(df: pd.DataFrame, plans: pd.DataFrame | None) -> pd.Series:
    factor = pd.Series(1.0, index=df.index)
    plan_map = None
    if plans is not None and not plans.empty and "id" in plans.columns:
        p = plans.copy()
        p["id"] = pd.to_numeric(p["id"], errors="coerce")
        plan_map = p[p["id"].notna()].set_index("id")

    for idx in df.index:
        f = None
        pid = df.at[idx, "plan_id_num"] if "plan_id_num" in df.columns else None
        if plan_map is not None and pd.notna(pid):
            try:
                pid_i = int(pid)
            except (TypeError, ValueError):
                pid_i = None
            if pid_i is not None and pid_i in plan_map.index:
                row = plan_map.loc[pid_i]
                if isinstance(row, pd.DataFrame):
                    row = row.iloc[0]
                f = plan_period_months(
                    row.get("billing_period_value", 1),
                    row.get("billing_period_unit", "month"),
                )
        if f is None and "plan" in df.columns:
            f = _period_months_from_plan_name(df.at[idx, "plan"])
        factor.loc[idx] = float(f) if f is not None else 1.0
    return factor


def _valores_cierre(df: pd.DataFrame, factor: pd.Series) -> list[float]:
    """Prioridad: recurring_total → amount_untaxed → amount_signed × plan."""
    total = pd.to_numeric(df.get("recurring_total", 0), errors="coerce")
    untaxed = pd.to_numeric(df.get("amount_untaxed", 0), errors="coerce")
    amount = pd.to_numeric(df.get("amount_signed", 0), errors="coerce").fillna(0.0)
    mrr = pd.to_numeric(df.get("recurring_monthly", 0), errors="coerce").fillna(0.0)

    valores = []
    for i, idx in enumerate(df.index):
        t = float(total.loc[idx]) if pd.notna(total.loc[idx]) else 0.0
        u = float(untaxed.loc[idx]) if pd.notna(untaxed.loc[idx]) else 0.0
        a = float(amount.loc[idx])
        m = float(mrr.loc[idx])
        f = float(factor.loc[idx]) if factor.loc[idx] else 1.0

        if t > 0:
            valores.append(t)
            continue
        if u > 0:
            valores.append(u)
            continue
        # amount_signed es MRR (estándar Odoo) → × período del plan
        base = a if a else m
        if f > 1.01:
            # Si amount ya ≈ base×f, no remultiplicar
            if abs(a - base * f) / max(abs(base * f), 1.0) < 0.08 and a > base * 1.5:
                valores.append(a)
            else:
                valores.append(base * f)
        else:
            valores.append(base)
    return valores


def log_cierre_monthly(logs: pd.DataFrame, months: list[str],
                       team_id: int | None = None,
                       plans: pd.DataFrame | None = None,
                       subs: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cierre desde eventos New: importe del período del plan."""
    model = "sale.order.log.report"
    if logs is not None and not logs.empty and "_model" in logs.columns:
        model = str(logs["_model"].iloc[0])
    fuente = f"{model} · New · recurring_total / (MRR × plan)"
    if not months:
        return pd.DataFrame(columns=["mes", "vendido", "fuente"])
    empty = pd.DataFrame({"mes": months, "vendido": [0.0] * len(months),
                          "fuente": [fuente] * len(months)})
    if logs is None or logs.empty:
        return empty
    df = logs.copy()
    if "order_id_num" not in df.columns and "order_id" in df.columns:
        df["order_id_num"] = m2o_id(df["order_id"])
    if team_id is not None and "equipo_id" in df.columns:
        df = df[pd.to_numeric(df["equipo_id"], errors="coerce") == int(team_id)]
    if "event_type" in df.columns:
        df = df[df["event_type"] == "0_creation"]
    if df.empty:
        return empty

    df = _enrich_logs_from_subs(df, subs)

    if "first_contract_date" in df.columns and df["first_contract_date"].notna().any():
        df["_mes_src"] = df["first_contract_date"].fillna(df.get("event_date"))
    else:
        df["_mes_src"] = df["event_date"]
    df = df[df["_mes_src"].notna()].copy()
    if df.empty:
        return empty
    df["mes"] = pd.to_datetime(df["_mes_src"]).dt.to_period("M").astype(str)

    factor = _plan_factor_series(df, plans)
    df["periodo_plan_meses"] = factor
    df["_valor"] = _valores_cierre(df, factor)

    por_mes = df.groupby("mes", as_index=False)["_valor"].sum().rename(columns={"_valor": "vendido"})
    out = pd.DataFrame({"mes": months}).merge(por_mes, on="mes", how="left")
    out["vendido"] = out["vendido"].fillna(0.0)
    out["fuente"] = fuente
    return out


def subscription_cierre_from_subs(subs: pd.DataFrame, months: list[str],
                                  team_id: int | None = None,
                                  plans: pd.DataFrame | None = None) -> pd.DataFrame:
    """Fallback: recurring_total (o MRR × plan) por first_contract_date."""
    from odoo_io import _subscription_cierre_frame

    fuente = "sale.order · first_contract_date + recurring_total (período del plan)"
    if not months:
        return pd.DataFrame(columns=["mes", "vendido", "fuente"])
    empty = pd.DataFrame({"mes": months, "vendido": [0.0] * len(months),
                          "fuente": [fuente] * len(months)})
    sub = _subscription_cierre_frame(subs, team_id=team_id)
    if sub.empty:
        return empty
    sub = sub.copy()
    sub["mes"] = sub["first_contract_date"].dt.to_period("M").astype(str)
    if "plan_id_num" not in sub.columns and "plan_id" in sub.columns:
        sub["plan_id_num"] = m2o_id(sub["plan_id"])
    factor = _plan_factor_series(sub, plans)
    # Adaptar columnas al helper de valores
    if "amount_signed" not in sub.columns:
        sub["amount_signed"] = pd.to_numeric(sub.get("recurring_monthly", 0), errors="coerce").fillna(0.0)
    sub["_valor"] = _valores_cierre(sub, factor)
    por_mes = sub.groupby("mes", as_index=False)["_valor"].sum().rename(columns={"_valor": "vendido"})
    out = pd.DataFrame({"mes": months}).merge(por_mes, on="mes", how="left")
    out["vendido"] = out["vendido"].fillna(0.0)
    out["fuente"] = fuente
    return out


def staff_cierre_monthly(requests: pd.DataFrame | None, subs: pd.DataFrame | None,
                         months: list[str], team_id: int | None = None,
                         logs: pd.DataFrame | None = None,
                         plans: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cierre: log.report New (+ enrich SO) → fallback suscripciones → staffing."""
    if logs is not None and not logs.empty:
        out = log_cierre_monthly(
            logs, months, team_id=team_id, plans=plans, subs=subs
        )
        if out["vendido"].sum() > 0:
            return out
    if subs is not None and not subs.empty:
        return subscription_cierre_from_subs(subs, months, team_id=team_id, plans=plans)
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
