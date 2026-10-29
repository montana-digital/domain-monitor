import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.capabilities import refresh_capabilities
from app.checks.screenshot import shutdown_browser
from app.config import Settings
from app.db import init_db
from app.notices import notices
from app.routes.pages import LoginRequired, http_error, login_redirect, router, unhandled_error
from app.scheduler import prepare_schedule, shutdown_scheduler, start_scheduler
from app.service import begin_shutdown, shutdown_checks

logger = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parent / "static"


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-content-type-options", b"nosniff"))
                headers.append((b"referrer-policy", b"same-origin"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    init_db(settings)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.settings.start_scheduler:
            await asyncio.to_thread(refresh_capabilities)
            prepare_schedule()
            notices.scheduler_enabled = True
            start_scheduler()
        else:
            notices.scheduler_enabled = False
        try:
            yield
        finally:
            begin_shutdown()
            if app.state.settings.start_scheduler:
                shutdown_scheduler()
            await shutdown_checks()
            await shutdown_browser()

    app = FastAPI(title="Domain Monitor", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(SecurityHeaders)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        same_site="lax",
        https_only=settings.https_only,
        max_age=60 * 60 * 24 * 14,
    )
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    app.add_exception_handler(LoginRequired, login_redirect)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(Exception, unhandled_error)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = app.state.settings
    uvicorn.run("app.main:app", host=settings.host, port=settings.port, factory=False)


if __name__ == "__main__":
    main()
