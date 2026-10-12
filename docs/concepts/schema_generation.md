# Schema generation

Cadwyn automatically generates versioned schemas and everything related to them from HEAD version at runtime — no code is actually generated. These versioned schemas will be automatically used in requests to and responses from [versioned API routes](./main_app.md#main-app).

## Rendering schemas

If you have many versions and schemas, it is quite challenging to know what validators, fields, and other attributes are defined in each schema in a specific version. To address this issue, there is a way to **render** the generated Pydantic models and enums as code using the command-line interface.

**NOTICE** that `cadwyn render` does not guarantee rendering correct schemas. It provides a close enough approximation for manual validation, but it is not yet ready for production code generation. For example, it doesn't handle schema renaming in a class's `__bases__` yet.

### Rendering a module

Here's a way to render the entire module with the schemas:

```bash
cadwyn render module data.schemas --app=main:app --version=2024-05-26
```
<!-- TODO: Add an option to use a callable instead of a variable -->
This command prints to stdout how the schemas would look in version 2024-05-26 if they were written by hand rather than generated at runtime by Cadwyn. This command takes the schemas from `data/schemas.py` module and knows what the schemas would look based on the version changes from `Cadwyn` app instance named `app` and located in `main.py`.

### Rendering a single model

Here's a way to render a single Pydantic model or enum with the schemas:

```bash
cadwyn render model data.schemas:UserCreateRequest --app=main:app --version=2024-05-26
```

This command prints to stdout how the `UserCreateRequest` schema would look in version 2024-05-26 if it was written by hand rather than generated at runtime by Cadwyn. This command takes the `UserCreateRequest` schema from `data/schemas.py` module and knows what the schema would look based on the version changes from `Cadwyn` app instance named `app` and located in `main.py`.

## Generating schemas without FastAPI

Cadwyn can generate versioned schemas from its version changes even without FastAPI:

```python
import cadwyn
from cadwyn.schema_generation import generate_versioned_models
from my_versions import MyVersionedSchema, version_bundle

schema_generators = generate_versioned_models(version_bundle)
MyVersionedSchemaFrom2025 = schema_generators["2025-11-16"][MyVersionedSchema]
```

Cadwyn generates a copy of every model for every version, except for the enums without enum instructions and the models that refer to themselves (directly or through other models, e.g. `children: "list[Category]"`) when neither they nor the models they refer to have instructions in any version (including `HeadVersion`) and they are not parents of models with versioned copies. Cadwyn uses such enums and models as they are: `schema_generators["2025-11-16"][Category] is Category`. Cadwyn makes this decision when it first needs a model, so the models defined after that (e.g. new subclasses) do not change it.

### Models that refer to each other

Models can refer to themselves, directly or through other models, e.g. `children: "list[Category]"` or `Author.books: list[Book]` with `Book.author: Author`. When such models need versioning, Cadwyn copies all of them together, and their copies refer to each other. So if `Category` had a `title` field instead of `name` in an older version, that version uses `title` at every depth of the tree. This requires Pydantic 2.11.5 or newer: with older Pydantic versions, Cadwyn raises a `SchemaGenerationError` for such models.

!!! warning "Generic models keep the latest models that refer to each other"

    In older versions, the type args of parametrized generic models stay the latest models if they refer to models that refer to each other and need versioning. This applies to generic route models such as `response_model=Page[Category]`, to their subclasses such as `class CategoryPage(Page[Category])`, to `RootModel[list[Category]]`, and to generic fields such as `children: Page["Category"]`. Older versions validate and serialize the models inside such generics in their latest shape, so their clients need converters for these routes, e.g. `convert_response_to_previous_version_for(Page[Category])`.

Cadwyn raises a `SchemaGenerationError` when it generates the versions if it cannot copy such models:

* when they refer to each other in places that need the classes themselves rather than annotations, e.g. in default factories, in the metadata of `Annotated`, in type aliases, or in the type args of generics that are not from `typing` or `collections.abc`;
* when the return type of a computed field of one of them refers to models that need versioning, because Cadwyn does not version the return types of computed fields;
* when their annotations, or the annotations of the functions that they use, refer to names that are not defined yet.

Some other things to keep in mind about such models:

* Cadwyn does not see the names quoted inside other annotations, such as `list["Book"]` or `Optional[ForwardRef("Book")]`. Pydantic resolves them in the module of the model, so they refer to the latest models, except for the name of the model itself, which refers to its own copy. Quote the whole annotation instead: `"list[Book]"`.
* Their copies are built when Cadwyn generates them, even if they use `defer_build=True`.
* Their hooks that Pydantic calls while it builds them, such as `__pydantic_init_subclass__` or `__get_pydantic_core_schema__`, must not use Cadwyn's versioned models (e.g. through `generate_versioned_models` or `migrate_response_body`): Cadwyn raises a `CadwynError` if they ask for the models that it is copying, and on newer Pydantic versions they can deadlock with other threads that generate versioned models.
