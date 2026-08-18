# 📋 Dashboard Staff, Formación y Fábrica de Software

Tablero ejecutivo conectado en vivo a Odoo 19 (XML-RPC) para las líneas
**Staff** (staffing), **Formación** y **Fábrica de Software**. Nace de la
reunión con Raquel Cañón / Paula López (2026-08) para dejar de depender de
Excel en estos informes.

## Cómo correrlo localmente

Prerrequisito: instalar `uv` (`curl -LsSf https://astral.sh/uv/install.sh | sh`).

```
$ uv sync
$ cp .streamlit/secrets.toml.example .streamlit/secrets.toml   # y completa tus credenciales
$ uv run streamlit run streamlit_app.py
```

## Configuración de datos

- `data/metas_lineas.csv` — meta de venta **anual** por línea (`linea,meta_anual`).
  Los nombres de línea deben coincidir EXACTAMENTE con las claves de
  `LINEA_TEAM` en `streamlit_app.py` (Staff / Formación / Fábrica de Software).
- `data/costos_fijos.csv` — costo fijo mensual por línea (`linea,persona,costo_mensual`),
  usado en el cálculo de rentabilidad (ej. costo de Diego en Staff, Paula en Formación).

## Supuestos técnicos a verificar contra la Odoo real

Este dashboard reutiliza patrones ya probados en producción en el repo
hermano `gdp-dashboard-1` (mismo Odoo, mismo cliente), pero hay 3 piezas
nuevas que no estaban validadas antes y pueden necesitar un ajuste de
nombre de campo/modelo la primera vez que corra contra Odoo real:

1. **Línea = equipo de venta (`crm.team`)** — según lo acordado en la
   reunión (equipos, no etiquetas). `LINEA_TEAM` en el código asume que los
   equipos se llaman exactamente "Staff", "Formación" y "Fábrica de
   Software" — ajusta el diccionario si los nombres reales son otros.
2. **Plazas activas de Staff** = # de `sale.order` con
   `is_subscription=True` y `subscription_state='3_progress'` del equipo
   Staff (cada suscripción = 1 plaza, tal como se decidió). Si una
   suscripción agrupa varias plazas en una sola línea de producto, hay que
   cambiar el conteo por `product_uom_qty`.
3. **Horas y costos del equipo** = `account.analytic.line`, asumiendo una
   cuenta analítica por línea con el mismo nombre que la línea
   (`LINEA_ANALYTIC`). Si aún no existen esas cuentas analíticas, la
   pestaña Equipo y la sección de Rentabilidad de cada línea muestran un
   aviso en vez de romper.

Si algo de esto truena contra la Odoo real, el mensaje de error dice qué
modelo/campo revisar.

## Cosas explícitamente pendientes / fuera de alcance de este dashboard

De las notas de la reunión, esto **no** está resuelto todavía y necesita
trabajo aparte (probablemente un módulo/config en Odoo, no en este repo):

- Automatizar la creación de cuentas analíticas al generar cada suscripción
  (para vincular consistentemente facturas de proveedor y de cliente).
- Campo dedicado en la suscripción/orden para identificar el recurso de
  staffing (hoy va en texto libre en la descripción).
- Reclasificación de leads de Staff vs. Fábrica de Software en el equipo
  de ventas (a cargo de Paula).
- Fuente de datos real para las **proyecciones** de cursos vendidos
  (Formación) y proyectos vendidos (Fábrica) — el dashboard usa mientras
  tanto un promedio móvil simple marcado como "provisional" en la UI.
- "Horas dedicadas a cada tipo de actividad" (ej. cuántas horas le dedica
  alguien a hacer propuestas) no se puede sacar de `mail.activity` (no
  tiene duración y Odoo la borra al completarla) — necesitaría un registro
  dedicado si se quiere ese detalle.
