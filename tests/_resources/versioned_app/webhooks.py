from fastapi.routing import APIRouter

router = APIRouter(prefix="/v1")


@router.post("/unversioned", response_model=dict)
def read_root():
    return {"saved": True}
