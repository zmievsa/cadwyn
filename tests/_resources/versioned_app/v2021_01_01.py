from fastapi.routing import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/v1")


class FirstVersionResponseModel(BaseModel):
    my_version1: int


@router.get("", response_model=FirstVersionResponseModel)
def read_root():
    return {"my_version1": 1}


@router.websocket_route("/non_api_route_made_only_to_verify_that_we_can_have_non_api_routes")
def non_api_route_made_only_to_verify_that_we_can_have_non_api_routes():
    raise NotImplementedError
