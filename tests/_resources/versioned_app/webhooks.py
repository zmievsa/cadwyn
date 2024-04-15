from fastapi.routing import APIRouter

router = APIRouter(prefix="/v1")


@router.post("/webhooks", response_model=dict)
def read_root():
    return {"saved": True}
