import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress

from starlette.applications import Starlette
from starlette.middleware import Middleware

from . import config as C
from .config import Settings
from .maintenance import loop as maintenance_loop
from .routes import ResponseBoundary, ROUTES
from .service import Publisher


def create_app(settings=None, clock=None):
    @asynccontextmanager
    async def lifespan(app):
        pool = ThreadPoolExecutor(max_workers=C.THREAD_WORKERS, thread_name_prefix="edge-io")
        asyncio.get_running_loop().set_default_executor(pool)
        service = Publisher(settings or Settings.from_env(), clock)
        app.state.publisher = service
        maintenance = None
        try:
            await service.start()
            maintenance = asyncio.create_task(maintenance_loop(service))
            yield
        finally:
            if maintenance is not None:
                maintenance.cancel()
                with suppress(asyncio.CancelledError):
                    await maintenance
            await service.stop()
            pool.shutdown(wait=False, cancel_futures=True)

    application = Starlette(routes=ROUTES, lifespan=lifespan,
                            middleware=[Middleware(ResponseBoundary)])
    application.router.redirect_slashes = False
    return application


app = create_app()
