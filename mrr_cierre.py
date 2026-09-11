# -*- coding: utf-8 -*-
"""Cierre Staff: importe de un período del plan por Fecha del primer contrato.

Fuente principal: suscripciones (sale.order) — estable mes a mes.
Apoyo: sale.order.log.report (eventos New) para detalle / cruce.

Valor por contrato:
  1) recurring_total (importe del período en Odoo)
  2) recurring_monthly × billing_period del plan
  3) amount_untaxed
Nunca × vigencia calendario start→end.
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
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return None
    s = str(name).strip().lower()
    if not s or s in ("sin asignar", "false", "none"):
        return None
    if "trimestr" in s or "quarter" in s:
        return 3.0
    if "semestr" in s or "semester" in s:
        return 6.0
    if "anual" in s or "annual" in s or "yearly" in s:
        return 12.0
    m = re.search(r"(\d+)\s*(mes|month)", s)
    if m:
        return float(m.group(1))
    if "mensual" in s or "monthly" in s or re.search(r"\bmonth\b", s):
        return 1.0
    return None


@st.cache_data(ttl=600, show_spinner="Cargando MRR Breakdown (sale.order.log.report)...")
def load_sale_order_log_report(date_from: str, date_to: str, team_ids: list[int]):
    """Detalle New desde sale.order.log.report (fallback sale.order.log)."""
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
    fields = list(pick_fields(model, cols) if have else cols)
    for must in ("event_type", "event_date", "order_id", "amount_signed", "team_id"):
        if must not in fields:
            fields.append(must)
    if "plan_id" not in fields:
        fields.append("plan_id")

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
    if df.empty or "id" not in df.columns:
        return pd.DataFrame(columns=["id", "name", "billing_period_value", "billing_period_unit"])
    df["id"] = pd.to_numeric(df["id"], errors="coerce")
    df["billing_period_value"] = pd.to_numeric(df.get("billing_period_value", 1), errors="coerce").fillna(1.0)
    df["billing_period_unit"] = df.get("billing_period_unit", "month").fillna("month").astype(str)
    return df


def _plan_factor_series(df: pd.DataFrame, plans: pd.DataFrame | None) -> pd.Series:
    factor = pd.Series(1.0, index=df.index)
    plan_map = None
    if plans is not None and not plans.empty and "id" in plans.columns:
        p = plans.copy()
        p["id"] = pd.to_numeric(p["id"], errors="coerce")
        plan_map = p[p["id"].notna()].drop_duplicates(subset=["id"]).set_index("id")

    for idx in df.index:
        f = None
        pid = df.at[idx, "plan_id_num"] if "plan_id_num" in df.columns else None
        if plan_map is not None and pd.notna(pid):
            try:
                pid_i = int(float(pid))
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


def _valor_contrato_row(recurring_total, amount_untaxed, mrr, factor) -> float:
    """Importe de un período del plan para una suscripción."""
    try:
        t = float(recurring_total) if recurring_total is not None and not pd.isna(recurring_total) else 0.0
    except (TypeError, ValueError):
        t = 0.0
    try:
        u = float(amount_untaxed) if amount_untaxed is not None and not pd.isna(amount_untaxed) else 0.0
    except (TypeError, ValueError):
        u = 0.0
    try:
        m = float(mrr) if mrr is not None and not pd.isna(mrr) else 0.0
    except (TypeError, ValueError):
        m = 0.0
    f = float(factor) if factor else 1.0

    if t > 0:
        return t
    # MRR × período del plan (8×3=24). Preferir esto a untaxed si untaxed ≈ MRR y el plan es >1 mes.
    if m > 0 and f > 1.01:
        periodo = m * f
        if u > 0 and abs(u - periodo) / max(periodo, 1.0) < 0.08:
            return u  # untaxed ya es el período
        if u > 0 and abs(u - m) / max(m, 1.0) < 0.08:
            return periodo  # untaxed es solo 1 mes de MRR → escalar
        return periodo
    if u > 0:
        return u
    if m > 0:
        return m * max(f, 1.0)
    return 0.0


def subscription_cierre_from_subs(subs: pd.DataFrame, months: list[str],
                                  team_id: int | None = None,
                                  plans: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cierre mes a mes: recurring_total / (MRR×plan) por first_contract_date."""
    from odoo_io import _subscription_cierre_frame

    fuente = "sale.order · first_contract_date + recurring_total (o MRR × plan)"
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
    # Solo contratos cuyo primer contrato cae en el rango de meses del filtro
    sub["mes"] = sub["first_contract_date"].dt.to_period("M").astype(str)
    sub = sub[sub["mes"].isin(months)].copy()
    if sub.empty:
        return empty

    if "plan_id_num" not in sub.columns and "plan_id" in sub.columns:
        sub["plan_id_num"] = m2o_id(sub["plan_id"])
    factor = _plan_factor_series(sub, plans)
    sub["periodo_plan_meses"] = factor
    sub["valor_cierre"] = [
        _valor_contrato_row(
            sub.at[i, "recurring_total"] if "recurring_total" in sub.columns else 0,
            sub.at[i, "amount_untaxed"] if "amount_untaxed" in sub.columns else 0,
            sub.at[i, "recurring_monthly"] if "recurring_monthly" in sub.columns else 0,
            factor.at[i],
        )
        for i in sub.index
    ]
    por_mes = sub.groupby("mes", as_index=False)["valor_cierre"].sum().rename(
        columns={"valor_cierre": "vendido"}
    )
    out = pd.DataFrame({"mes": months}).merge(por_mes, on="mes", how="left")
    out["vendido"] = out["vendido"].fillna(0.0)
    out["fuente"] = fuente
    return out


def log_cierre_monthly(logs: pd.DataFrame, months: list[str],
                       team_id: int | None = None,
                       plans: pd.DataFrame | None = None,
                       subs: pd.DataFrame | None = None) -> pd.DataFrame:
    """Apoyo: eventos New del log, enriquecidos con la OV."""
    model = "sale.order.log.report"
    if logs is not None and not logs.empty and "_model" in logs.columns:
        model = str(logs["_model"].iloc[0])
    fuente = f"{model} · New (apoyo)"
    if not months:
        return pd.DataFrame(columns=["mes", "vendido", "fuente"])
    empty = pd.DataFrame({"mes": months, "vendido": [0.0] * len(months),
                          "fuente": [fuente] * len(months)})
    if logs is None or logs.empty:
        return empty
    df = logs.copy()
    if "order_id_num" not in df.columns and "order_id" in df.columns:
        df["order_id_num"] = m2o_id(df["order_id"])
    # Filtrar equipo solo si la mayoría de filas trae team_id (si no, no vaciar el set).
    if team_id is not None and "equipo_id" in df.columns:
        eq = pd.to_numeric(df["equipo_id"], errors="coerce")
        if eq.notna().mean() >= 0.5:
            df = df[eq == int(team_id)]
    if "event_type" in df.columns:
        df = df[df["event_type"] == "0_creation"]
    if df.empty:
        return empty

    # Enrich desde suscripciones
    if subs is not None and not subs.empty and "id" in subs.columns:
        sub = subs.copy()
        sub["id"] = pd.to_numeric(sub["id"], errors="coerce")
        sub = sub[sub["id"].notna()].drop_duplicates(subset=["id"]).set_index("id")
        totals, untaxeds, mrrs, plans_n, plans_name = [], [], [], [], []
        for oid in pd.to_numeric(df["order_id_num"], errors="coerce"):
            if pd.isna(oid) or oid not in sub.index:
                totals.append(0.0)
                untaxeds.append(0.0)
                mrrs.append(0.0)
                plans_n.append(None)
                plans_name.append("")
                continue
            row = sub.loc[oid]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            totals.append(float(pd.to_numeric(row.get("recurring_total", 0), errors="coerce") or 0))
            untaxeds.append(float(pd.to_numeric(row.get("amount_untaxed", 0), errors="coerce") or 0))
            mrrs.append(float(pd.to_numeric(row.get("recurring_monthly", 0), errors="coerce") or 0))
            pid = row.get("plan_id")
            if isinstance(pid, (list, tuple)) and pid:
                try:
                    plans_n.append(int(pid[0]))
                except (TypeError, ValueError):
                    plans_n.append(None)
                plans_name.append(str(pid[1]) if len(pid) > 1 else "")
            else:
                try:
                    plans_n.append(int(pid) if pd.notna(pid) else None)
                except (TypeError, ValueError):
                    plans_n.append(None)
                plans_name.append(str(row.get("plan", "") or ""))
        df["recurring_total"] = totals
        df["amount_untaxed"] = untaxeds
        if "recurring_monthly" not in df.columns or df["recurring_monthly"].isna().all():
            df["recurring_monthly"] = mrrs
        if "plan_id_num" not in df.columns:
            df["plan_id_num"] = plans_n
        else:
            miss = pd.to_numeric(df["plan_id_num"], errors="coerce").isna()
            df.loc[miss, "plan_id_num"] = pd.Series(plans_n, index=df.index)[miss]
        blank_plan = df.get("plan", pd.Series("", index=df.index)).astype(str).isin(["", "Sin asignar", "False", "nan"])
        if "plan" not in df.columns:
            df["plan"] = plans_name
        else:
            df.loc[blank_plan, "plan"] = pd.Series(plans_name, index=df.index)[blank_plan]

    if "first_contract_date" in df.columns and df["first_contract_date"].notna().any():
        df["_mes_src"] = df["first_contract_date"].fillna(df.get("event_date"))
    else:
        df["_mes_src"] = df["event_date"]
    df = df[df["_mes_src"].notna()].copy()
    if df.empty:
        return empty
    df["mes"] = pd.to_datetime(df["_mes_src"]).dt.to_period("M").astype(str)
    df = df[df["mes"].isin(months)].copy()
    if df.empty:
        return empty

    factor = _plan_factor_series(df, plans)
    amount = pd.to_numeric(df.get("amount_signed", 0), errors="coerce").fillna(0.0)
    mrr = pd.to_numeric(df.get("recurring_monthly", 0), errors="coerce").fillna(0.0)
    # Si el log trae amount_signed, usarlo como MRR base cuando no hay recurring_monthly
    mrr = mrr.where(mrr > 0, amount)
    df["valor_cierre"] = [
        _valor_contrato_row(
            df.at[i, "recurring_total"] if "recurring_total" in df.columns else 0,
            df.at[i, "amount_untaxed"] if "amount_untaxed" in df.columns else 0,
            mrr.at[i],
            factor.at[i],
        )
        for i in df.index
    ]
    por_mes = df.groupby("mes", as_index=False)["valor_cierre"].sum().rename(
        columns={"valor_cierre": "vendido"}
    )
    out = pd.DataFrame({"mes": months}).merge(por_mes, on="mes", how="left")
    out["vendido"] = out["vendido"].fillna(0.0)
    out["fuente"] = fuente
    return out


def staff_cierre_monthly(requests: pd.DataFrame | None, subs: pd.DataFrame | None,
                         months: list[str], team_id: int | None = None,
                         logs: pd.DataFrame | None = None,
                         plans: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cierre: suscripciones (principal) → log.report apoyo → staffing."""
    out_subs = pd.DataFrame()
    if subs is not None and not subs.empty:
        out_subs = subscription_cierre_from_subs(subs, months, team_id=team_id, plans=plans)

    out_logs = pd.DataFrame()
    if logs is not None and not logs.empty:
        out_logs = log_cierre_monthly(
            logs, months, team_id=team_id, plans=plans, subs=subs
        )

    sum_s = float(out_subs["vendido"].sum()) if not out_subs.empty else 0.0
    sum_l = float(out_logs["vendido"].sum()) if not out_logs.empty else 0.0

    # Principal: suscripciones (cubre todos los meses con first_contract_date).
    # Si el log aporta más en algún mes (importe de período), tomar el máximo mes a mes.
    if sum_s > 0 and sum_l > 0:
        m = out_subs.merge(out_logs, on="mes", how="outer", suffixes=("_s", "_l"))
        m["vendido_s"] = m.get("vendido_s", 0).fillna(0.0)
        m["vendido_l"] = m.get("vendido_l", 0).fillna(0.0)
        m["vendido"] = m[["vendido_s", "vendido_l"]].max(axis=1)
        m["fuente"] = "sale.order + log.report (max mes)"
        # Reordenar a months
        out = pd.DataFrame({"mes": months}).merge(m[["mes", "vendido", "fuente"]], on="mes", how="left")
        out["vendido"] = out["vendido"].fillna(0.0)
        out["fuente"] = out["fuente"].fillna("sale.order + log.report (max mes)")
        return out
    if sum_s > 0:
        return out_subs
    if sum_l > 0:
        return out_logs
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
    "subscription_cierre_from_subs",
]
