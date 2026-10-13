from typing import Annotated

from cadwyn import Cadwyn, Version, VersionBundle, VersionedAPIRouter
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")
router = VersionedAPIRouter()


@router.post("/token")
async def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
):
    # Check the credentials and issue a real token here
    return {"access_token": form_data.username, "token_type": "bearer"}


@router.get("/users/me")
async def read_current_user(token: Annotated[str, Depends(oauth2_scheme)]):
    return {"token": token}


app = Cadwyn(
    versions=VersionBundle(Version("2000-01-01")),
    # Requests without the version header get this version
    api_version_default_value="2000-01-01",
)
app.generate_and_include_versioned_routers(router)
