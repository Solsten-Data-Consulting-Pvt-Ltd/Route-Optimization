import logging
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.schemas import SaveConsignmentsRequest
from app.services.save import save_consignments_pipeline

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/save-consignments")
def save_consignments(
    payload: SaveConsignmentsRequest = SaveConsignmentsRequest(),
    consignmentId: Optional[str] = Query(default=None),
):
    consignment_ids = payload.consignmentIds
    if not consignment_ids:
        single = payload.consignmentId or consignmentId
        consignment_ids = [single] if single else []

    if not consignment_ids:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "consignmentIds (or consignmentId) is required"},
        )

    if not isinstance(consignment_ids, list) or not all(isinstance(c, str) for c in consignment_ids):
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "consignmentIds must be a list of strings"},
        )

    try:
        result = save_consignments_pipeline(consignment_ids)
        status = "ok" if result["failed"] == 0 else "partial_success"
        return {"status": status, **result}
    except Exception as e:
        logger.exception("save_consignments pipeline failed")
        return JSONResponse(status_code=500, content={"status": "error", "message": str(e)})
