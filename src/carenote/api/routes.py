"""The `/api` router, assembled from one router per area."""

from fastapi import APIRouter

from carenote.api import agent, auth, knowledge, patients, system

router = APIRouter(prefix="/api")
for module in (system, auth, patients, agent, knowledge):
    router.include_router(module.router)
router.include_router(patients.realtime_router)
