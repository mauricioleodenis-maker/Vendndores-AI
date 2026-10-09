"""Menu de navegacion completo (MAESTRO §8 + support/15 §2). Un item por pagina."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NavItem:
    label: str
    href: str
    roles: tuple[str, ...] | None = None  # None = todos los autenticados; owner siempre ve todo
    children: tuple[NavItem, ...] = ()


NAV_ITEMS: tuple[NavItem, ...] = (
    NavItem("Inicio", "/admin/inicio"),
    NavItem(
        "Empresas",
        "/admin/negocios",
        ("admin",),
        (NavItem("Nueva empresa", "/admin/negocios/nuevo", ("admin",)),),
    ),
    NavItem("Conversaciones", "/admin/conversaciones"),
    NavItem("Citas", "/admin/citas"),
    NavItem("Recordatorios", "/admin/recordatorios"),
    NavItem("Leads", "/admin/leads"),
    NavItem("Campañas", "/admin/campanas"),
    NavItem(
        "Planes",
        "/admin/planes",
        ("admin",),
        (NavItem("Facturación", "/admin/facturacion", ("admin",)),),
    ),
    NavItem("Kit de ventas", "/admin/kit-ventas"),
    NavItem("Auditoría", "/admin/auditoria", ()),  # solo owner
    NavItem("Ajustes", "/admin/ajustes"),
)

# Paginas de primer nivel que tendran un marcador si ningun modulo las registra.
PLACEHOLDER_PATHS: tuple[str, ...] = (
    "/admin/inicio",
    "/admin/negocios",
    "/admin/conversaciones",
    "/admin/citas",
    "/admin/leads",
    "/admin/campanas",
    "/admin/planes",
    "/admin/facturacion",
    "/admin/kit-ventas",
)


def _visible(item: NavItem, role: str | None) -> bool:
    if role == "owner":
        return True
    return item.roles is None or (role is not None and role in item.roles)


def nav_for_role(role: str | None) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for item in NAV_ITEMS:
        if not _visible(item, role):
            continue
        out.append(
            {
                "label": item.label,
                "href": item.href,
                "children": [
                    {"label": c.label, "href": c.href} for c in item.children if _visible(c, role)
                ],
            }
        )
    return out
