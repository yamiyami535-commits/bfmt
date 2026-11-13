from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.cr_poll_worker import start_cr_poll_worker, stop_cr_poll_worker
from app.jobs import start_recovery_worker, stop_recovery_worker
from app.routers import admin, audit_logs, auth, bots, integrations, ticket_insert, ticket_insert_external
from app.schema_bootstrap import ensure_hub_tables

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ = get_settings()
    ensure_hub_tables()
    start_recovery_worker()
    start_cr_poll_worker()
    yield
    stop_cr_poll_worker()
    stop_recovery_worker()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    if settings.debug:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(bots.router)
    app.include_router(audit_logs.router)
    app.include_router(integrations.router)
    app.include_router(ticket_insert.router)
    app.include_router(ticket_insert_external.router)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/admin", include_in_schema=False)
    def admin_login_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "login.html")

    @app.get("/admin/dashboard", include_in_schema=False)
    def admin_dashboard_redirect() -> RedirectResponse:
        return RedirectResponse(url="/admin/app", status_code=302)

    @app.get("/admin/app", include_in_schema=False)
    def admin_app_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "dashboard.html")

    @app.get("/docs", include_in_schema=False)
    def custom_swagger_ui_html() -> HTMLResponse:
        response = get_swagger_ui_html(
            openapi_url=str(app.openapi_url),
            title=f"{settings.app_name} - API docs",
        )
        html = bytes(response.body).decode("utf-8")
        extra = """
        <style>
        .topbar .custom-back-link { color: #fff; font-weight: 600; margin-left: 1rem; text-decoration: none; font-size: 0.95rem; }
        .topbar .custom-back-link:hover { text-decoration: underline; }
        </style>
        <script>
        window.addEventListener("load", () => {
          const addLink = () => {
            const topbar = document.querySelector(".topbar");
            if (!topbar) return;
            const customLink = topbar.querySelector(".custom-back-link");
            if (customLink) return;
            const a = document.createElement("a");
            a.href = "/admin/dashboard";
            a.className = "custom-back-link";
            a.textContent = "← Back to dashboard";
            topbar.appendChild(a);
            const openapiLink = topbar.querySelector("a[href$='/openapi.json']");
            if (openapiLink) {
              openapiLink.href = "/admin/dashboard";
              openapiLink.textContent = "Back to dashboard";
            }
          };
          addLink();
          const obs = new MutationObserver(addLink);
          obs.observe(document.documentElement, { childList: true, subtree: true });
        });
        </script>
        """
        return HTMLResponse(html.replace("</head>", extra + "</head>"))

    @app.get("/docs/oauth2-redirect", include_in_schema=False)
    async def swagger_ui_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    return app


app = create_app()
