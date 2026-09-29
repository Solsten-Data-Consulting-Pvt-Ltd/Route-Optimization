from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class GeocodePreviewRequest(BaseModel):
    receiverName: Optional[str] = None
    receiverAddress: str
    # Optional: lets the preview reuse a pin already resolved for the same
    # receiver/place in this DRS. With consignmentId the backend reads
    # receiver.phone / fullAddress from the consignment itself.
    drsId: Optional[str] = None
    consignmentId: Optional[str] = None
    # Optional OCR address components 3PL already has on the save screen
    # (premise, sub_locality/locality, city, postal_code), used for the one
    # shortened-address retry when Places finds nothing.
    addressComponents: Optional[Dict[str, Any]] = None


class ConfirmedLocation(BaseModel):
    """A point the executive has already seen on the map and accepted
    (corrected=False) or dragged/placed themselves (corrected=True)."""
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    formatted_address: Optional[str] = None
    corrected: bool = False
    overriddenBy: Optional[str] = None  # executiveId; required when corrected=True
    # Address resolution (all optional; older app builds send none of them).
    resolution: Optional[str] = None   # from the preview the executive accepted
    source: Optional[str] = None       # "preview" | "confirmed" | "admin" (admin fix)
    pincode: Optional[str] = None      # the preview's pincode, so the row isn't blank

    @model_validator(mode="after")
    def _require_overridden_by_when_corrected(self):
        if self.corrected and not (self.overriddenBy or "").strip():
            raise ValueError("overriddenBy is required when corrected is true")
        return self


class SaveConsignmentsRequest(BaseModel):
    consignmentIds: Optional[List[str]] = None
    consignmentId: Optional[str] = None
    # Keyed by consignmentId. Only sent for consignments where the app already
    # has a confirmed point; those rows skip geocoding entirely.
    confirmedLocations: Optional[Dict[str, ConfirmedLocation]] = None


class RunSortingRequest(BaseModel):
    drsno: Optional[str] = Field(default=None)
