from sqlalchemy import Column, Integer, String, Table
from sqlalchemy.orm import registry
from sqlmodel import Field, SQLModel


class ModelFields(SQLModel):
    name: str


class MappedModel(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    name: str


class MappedChild(ModelFields, table=True):
    id: int = Field(default=1, primary_key=True)


class ImperativelyMappedModel:
    id: int
    name: str


mapper_registry = registry()
mapper_registry.map_imperatively(
    ImperativelyMappedModel,
    Table(
        "imperative_user",
        mapper_registry.metadata,
        Column("id", Integer, primary_key=True),
        Column("name", String),
    ),
)
