from datetime import date

from fastapi import Depends
from fastapi.security import OAuth2AuthorizationCodeBearer
from pydantic_settings import BaseSettings

import zata.head
from cadwyn import Cadwyn, VersionBundle
from cadwyn.structure import HeadVersion
from cadwyn.structure.versions import Version


class Settings(BaseSettings):
    openapi_redirect_url: str | None = None
    openapi_client_id: str | None = None
    scope_name: str | None = None


settings = Settings()


versions = VersionBundle(HeadVersion(), Version(date(2022, 11, 16)), head_schemas_package=zata.head)
app = Cadwyn(
    versions=versions,
    dependencies=[Depends(OAuth2AuthorizationCodeBearer("/auth", "/token"))],
    swagger_ui_oauth2_redirect_url=settings.openapi_redirect_url,  # /oauth-redirect
    swagger_ui_init_oauth={
        "usePkceWithAuthorizationCodeGrant": True,
        "clientId": settings.openapi_client_id,  # Azure client ID for my OpenAPI SPA
        "scopes": settings.scope_name,  # Scope name, e.g.: user_impersonation
    },
)
