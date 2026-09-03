# 📋 Dashboard Staff, Formación y Fábrica de Software

Tablero ejecutivo conectado en vivo a Odoo 19 (XML-RPC) para las líneas
**Staff** (staffing), **Formación** y **Fábrica de Software**. Nace de la
reunión con Raquel Cañón / Paula López para dejar de depender de Excel.

La pestaña **Resumen** responde las 6 preguntas del informe:

1. ¿Estamos cumpliendo las metas de ventas anuales?
2. ¿Cuántas plazas activas hay y qué se espera a 6 meses?
3. ¿Las líneas son rentables?
4. ¿De dónde vienen las oportunidades?
5. ¿Están llegando leads a cada línea?
6. ¿El equipo dedica tiempo a cada línea y se ven resultados?

## Cómo correrlo localmente

Prerrequisito: instalar `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```
$ uv sync
$ cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # y completa tus credenciales
$ uv run streamlit run streamlit_app.py
```

## Configuración de datos

- `data/metas_lineas.csv` — meta de venta **anual** por línea (`linea,meta_anual`).
  Los nombres deben coincidir con las claves de `LINEA_TEAM` (Staff / Formación / Fábrica de Software).
- `data/costos_fijos.csv` — costo fijo mensual (`linea,persona,costo_mensual`).
  Diego en Staff y Paula en Formación. Hoy van en 0 hasta que Raquel confirme el valor.

## Cómo se clasifica una línea

En este orden, el primero que aplique:

1. Equipo de venta (`crm.team`): `Staffing IT`, `FORMACION`, `FABRICA SOFTWARE`
2. `sale.order.service_line` / `project.project.service_line` (`staff`, `training`, `software_factory`)
3. `staff_request_id` (módulo `firefly_staffing`) → Staff

Así se rescatan ventas y facturas aunque el equipo CRM aún no esté asignado.

Cuentas analíticas: `Staffing IT`, `Formación TI`, `Fábrica Software`.

## Fuentes Odoo por indicador

| Indicador | Modelo / campo |
|---|---|
| Plazas activas y proyección | `firefly.staffing.request` (`date_start`/`date_end`, estado confirmado). Fallback: `sale.order` con `is_subscription` |
| Renovaciones del mes | `firefly.staffing.history` (`event_type=renewal`). Fallback: `sale.order.log` transferencias |
| Rentabilidad Staff (recurso) | Valor mensual a cobrar − valor a pagar al proveedor − fijo de Diego |
| Vendido en pesos | `sale.order` confirmadas por **`date_order`**, **antes de impuestos** (`amount_untaxed`). No usa `create_date` ni cotizaciones en borrador |
| Facturación | `account.move` posted por **`invoice_date`**, **antes de impuestos** (`amount_untaxed_signed`; NC restan). Cuadra con Base imponible / Importe sin impuestos |
| Leads / origen | `crm.lead` + `source_id`, filtrado por equipo |
| Cierre vs. meta | OV confirmadas vs. `meta_anual / 12` |
| Cursos/proyectos vendidos | Oportunidades `won_status=won` |
| Proyección 6 meses (Formación/Fábrica) | Pipeline abierto con `date_deadline` |
| Cursos/proyectos entregados | `project.project.service_line` (fecha fin = proxy; falta campo de entrega) |
| Actividades hechas | `crm.activity.report` (chatter con tipo de actividad) |
| Horas por línea / persona | `account.analytic.line` (`unit_amount`) |
| Horas por tipo de actividad | `calendar.event.duration` ligado a oportunidad (proxy) |

## Pendientes que no se resuelven en este repo

- **JUAN Z:** campo de fecha de entrega de capacitaciones. Mientras tanto se usa
  `project.project.date` (fin) → `date_start` → `create_date`.
- **PAULA / Raquel:** valor real del costo fijo de Diego (Staff) y Paula (Formación).
- **PAULA:** asignar equipo de venta en OV/facturas Staff; completar `date_deadline`
  en el pipeline; revisar orígenes/campañas y actividades de Formación y Staff.
- `mail.activity` no guarda duración y Odoo la borra al completar. Las horas por
  tipo de actividad son un proxy de calendario, no un timesheet por actividad.
