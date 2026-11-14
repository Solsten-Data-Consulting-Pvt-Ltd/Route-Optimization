from fastapi import APIRouter

from app.routers import cache, geocode, save, sorting

api_router = APIRouter()
api_router.include_router(cache.router)
api_router.include_router(geocode.router)
api_router.include_router(save.router)
api_router.include_router(sorting.router)
