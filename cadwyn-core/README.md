# cadwyn-core

The parts of [Cadwyn](https://github.com/zmievsa/cadwyn) that work without FastAPI: versions, version changes, data migrations, and the models of every version.

It only needs Pydantic. Import everything from `cadwyn_core`, and the errors from `cadwyn_core.exceptions`. [Without FastAPI](https://github.com/zmievsa/cadwyn/blob/main/docs/how_to/migrate_data_outside_of_routes.md#without-fastapi) shows how to use it.

Install `cadwyn` to version a FastAPI app. It installs the same version of `cadwyn-core` and exports the same objects, with `SchemaGenerator` in `cadwyn.schema_generation` and the errors in `cadwyn.exceptions`.
