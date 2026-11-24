import logging

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.routers import api_router
from app.services import alerts, osrm

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

app = FastAPI(title="Route Optimization", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/osrm")
def health_osrm(response: Response, notify: bool = False):
    """Daily OSRM check (Cloud Scheduler, 07:00 IST). 200 = ok (or OSRM off),
    503 = failed - and a failure also goes to the Google Chat space. With
    ?notify=true an OK result is posted to the space too, so a missing
    morning message means the check itself did not run."""
    result = osrm.health_check()
    if result["status"] == "failed":
        response.status_code = 503
        alerts.send_alert("osrm_health_failed",
                          f"OSRM health check FAILED: {result['detail']}. "
                          "Fix before DRS creation starts.", force=True)
    elif result["status"] == "ok" and notify:
        alerts.send_alert("osrm_health_ok",
                          f"OSRM health check OK (test route {result['test_route_m']} m, "
                          f"{result['ms']} ms).", force=True)
    return result
