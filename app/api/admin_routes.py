"""Compatibility import for integration settings routes.

The route implementation lives in ``app.routers.integrations`` and is mounted
by the application there. Re-exporting the router avoids duplicate endpoints.
"""

from app.routers.integrations import router

__all__ = ["router"]