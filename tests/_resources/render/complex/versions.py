from pydantic import Field, conbytes

from cadwyn import Version, VersionBundle
from cadwyn.applications import Cadwyn
from cadwyn.route_generation import VersionedAPIRouter
from cadwyn.structure import VersionChange
from cadwyn.structure.enums import enum
from cadwyn.structure.schemas import schema

from .classes import A, AlmostEmptyEnum, ModelWithWeirdFields, MyEnum


class MyVersionChange(VersionChange):
    description = ""
    instructions_to_migrate_to_previous_version = (
        enum(MyEnum).didnt_have("baz"),
        enum(AlmostEmptyEnum).didnt_have("foo"),
        schema(A).had(name="NOT_A"),
        schema(ModelWithWeirdFields)
        .field("gaz")
        .existed_as(type=conbytes(strict=True), info=Field(min_length=3, title="Hewwo")),
    )


app = Cadwyn(
    versions=VersionBundle(
        Version("2001-01-01", MyVersionChange),
        Version("2000-01-01"),
    )
)

router = VersionedAPIRouter()


@router.post("/", response_model=ModelWithWeirdFields)
async def post(body: ModelWithWeirdFields): ...  # pragma: no cover


app.generate_and_include_versioned_routers(router)
