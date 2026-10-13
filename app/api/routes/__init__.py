from app.api.routes.exports import router as exports_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.posts import router as posts_router
from app.api.routes.sessions import router as sessions_router

ALL_ROUTERS = (jobs_router, posts_router, sessions_router, exports_router)

__all__ = ["ALL_ROUTERS", "exports_router", "jobs_router", "posts_router", "sessions_router"]
