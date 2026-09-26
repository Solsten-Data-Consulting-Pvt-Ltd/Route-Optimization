from typing import Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class GeocodePreviewRequest(BaseModel):
    receiverName: Optional[str] = None
    receiverAddress: str
    # Optional: lets the preview reuse a pin already resolved for the same
    # receiver/place in this DRS. With consignmentId the backend reads
    # receiver.phone / fullAddress from the consignment itself.
    drsId: Optional[str] = None
    consignmentId: Optional[str] = None


class ConfirmedLocation(BaseModel):
    """A point the executive has already seen on the map and accepted
    (corrected=False) or dragged/placed themselves (corrected=True)."""
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    formatted_address: Optional[str] = None
    corrected: bool = False
    overriddenBy: Optional[str] = None  # executiveId; required when corrected=True

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
