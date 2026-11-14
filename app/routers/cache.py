import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.schemas import (
    GeocodeCacheConfirmRequest,
    GeocodeCacheCorrectRequest,
    GeocodeCacheRestoreRequest,
)
from app.services import cache_verify

logger = logging.getLogger(__name__)
router = APIRouter()


def _run(operation):
    try:
        return operation()
    except (LookupError, ValueError) as exc:
        return JSONResponse(status_code=404, content={"status": "error", "message": str(exc)})
    except Exception as exc:
        logger.exception("geocode cache operation failed")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(exc)})


@router.post("/geocode-cache/confirm")
def confirm(payload: GeocodeCacheConfirmRequest):
    return _run(lambda: cache_verify.confirm(
        payload.consignmentId, payload.deliveryLocation.model_dump(), payload.actor,
    ))


@router.post("/geocode-cache/correct")
def correct(payload: GeocodeCacheCorrectRequest):
    return _run(lambda: cache_verify.correct(
        payload.consignmentId, payload.latitude, payload.longitude, payload.actor,
    ))


@router.post("/geocode-cache/restore")
def restore(payload: GeocodeCacheRestoreRequest):
    return _run(lambda: cache_verify.restore(payload.previous))
