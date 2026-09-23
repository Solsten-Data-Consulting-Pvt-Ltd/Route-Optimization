import logging

from fastapi import APIRouter

from app.schemas import GeocodePreviewRequest
from app.services.save import preview_geocode

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/geocode/preview")
def geocode_preview(payload: GeocodePreviewRequest):
    """Return a lat/long for an address without saving anything to
    BigQuery or consignments_routing. Always 200; failures come back as
    {"status": "failed", "error": ...}."""
    try:
        return preview_geocode(payload.receiverName, payload.receiverAddress)
    except Exception as e:
        logger.exception("geocode_preview failed")
        return {"status": "failed", "error": str(e)}
