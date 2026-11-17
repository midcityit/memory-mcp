"""Tenants, scoped API keys and RBAC (RCL-73).

The app is wired in ``memory_mcp.server.create_app``: ``build_admin_store`` (legacy
mode when it returns None), ``TenantService``, ``Authenticator`` + ``PrincipalMiddleware``,
``Audit``, ``MemoryOps`` and the ``/admin`` router from ``admin.rest``.
"""
