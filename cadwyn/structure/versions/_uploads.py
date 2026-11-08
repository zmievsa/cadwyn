import dataclasses
import os
import tempfile
from collections import Counter, defaultdict
from collections.abc import (
    Iterable,
    Iterator,
    Sequence,
)
from typing import (
    BinaryIO,
    TypeAlias,
    Union,
)

import anyio
from fastapi import Request as FastapiRequest
from fastapi import params
from fastapi._compat import (
    ModelField,
    is_bytes_or_nonable_bytes_annotation,
    is_bytes_sequence_annotation,
)
from fastapi.dependencies.models import Dependant
from fastapi.dependencies.utils import get_validation_alias
from fastapi.routing import APIRoute
from starlette.datastructures import FormData, UploadFile

from ._common import _logger
from ._dependencies import _get_dependency_overrides, _iter_dependencies


@dataclasses.dataclass(frozen=True)
class _UploadReadByFastapi:
    form_key: str
    is_sequence: bool
    upload: UploadFile  # Keeps `id(upload)` valid
    file: BinaryIO
    position: int


# Maps `id(upload)` to the upload with its form key, the kind of the param that read it, its file,
# and its position right after FastAPI has read it
_UploadPositions: TypeAlias = dict[int, _UploadReadByFastapi]


def _get_uploads_read_by_fastapi(
    route: APIRoute, head_route: APIRoute, head_dependant: Dependant, form: FormData
) -> _UploadPositions:
    """Return the uploaded files that FastAPI has read for the `bytes` file params of the versioned route.

    Before FastAPI 0.157.0, FastAPI reads these files to the end, so the head route would get them empty if we passed
    them on as is. Since then, FastAPI puts them back where they were, and rewinding them replays what the versioned
    route did with them.
    These params can be declared by the endpoint or by any of its dependencies. We only take the params
    that the head route also has, look the files up the same way FastAPI does, and only consider the spooled
    files that starlette creates.
    """
    uploads: _UploadPositions = {}
    # The endpoint's own params come first and in their order, and the files they read are recorded as before
    own_params = _get_bytes_file_params_of_fields(route.dependant.body_params)
    for alias, is_sequence in own_params:
        for value in _get_form_values(form, alias, is_sequence=is_sequence):
            if isinstance(value, UploadFile) and isinstance(value.file, tempfile.SpooledTemporaryFile):
                uploads[id(value)] = _UploadReadByFastapi(alias, is_sequence, value, value.file, value.file.tell())
    dependency_params = _get_bytes_file_params_of_dependencies(route, head_route, head_dependant) - set(own_params)
    # If a scalar and a sequence param read the same file, the head must pick it as the scalar to get it rewound
    for alias, is_sequence in sorted(dependency_params, key=lambda param: param[1]):
        for upload, position in _iter_uploads_read_to_the_end(form, alias, is_sequence=is_sequence):
            uploads.setdefault(id(upload), _UploadReadByFastapi(alias, is_sequence, upload, upload.file, position))
    return uploads


def _iter_uploads_read_to_the_end(form: FormData, alias: str, *, is_sequence: bool) -> Iterator[tuple[UploadFile, int]]:
    """Yield the open spooled files that FastAPI picks for the param, with their positions, if they are at the end.

    Before FastAPI 0.157.0, FastAPI reads these files to the end. A file that is elsewhere was moved by a
    dependency after that, e.g. to skip a header, so we leave it as it is. We check the end by seeking, which doesn't
    read the file.
    """
    for value in _get_form_values(form, alias, is_sequence=is_sequence):
        if (
            isinstance(value, UploadFile)
            and isinstance(value.file, tempfile.SpooledTemporaryFile)
            and not value.file.closed
        ):
            position = value.file.tell()
            end = value.file.seek(0, os.SEEK_END)
            value.file.seek(position)
            if position == end:
                yield value, position


def _get_bytes_file_params_of_dependencies(
    route: APIRoute, head_route: APIRoute, head_dependant: Dependant
) -> set[tuple[str, bool]]:
    """Return `(alias, is_sequence)` of the `bytes` file params of dependencies whose files the head can get again.

    FastAPI reads the body params of every dependency, including the cached ones, so we walk the whole trees.
    We only take the params that the head's tree also has, and none at all if the versioned route has
    dependencies that the head doesn't have.
    """
    dependencies = list(_iter_dependencies(route.dependant, _get_dependency_overrides(route)))
    bytes_file_params = {
        param
        for _, dependant in dependencies
        if dependant is not None
        for param in _get_bytes_file_params_of_fields(dependant.body_params)
    }
    if not bytes_file_params:
        return bytes_file_params
    head_dependencies = list(_iter_dependencies(head_dependant, _get_dependency_overrides(head_route)))
    bytes_file_params &= {
        param
        for dependant in [head_dependant, *(dependant for _, dependant in head_dependencies)]
        if dependant is not None
        for param in _get_bytes_file_params_of_fields(dependant.body_params)
    }
    # Counted, so that a versioned route that runs a dependency more often than the head also counts as adding it
    head_call_keys = Counter(call_key for call_key, _ in head_dependencies)
    # A dependency that only the versioned route has won't run again for the head, so a rewind would undo what it
    # did with a file. It can reach any file, e.g. through `Request.form()` or a sub-dependency that the head also
    # has, so we leave the files of all dependencies as they are
    if Counter(call_key for call_key, _ in dependencies) - head_call_keys:
        return set()
    return bytes_file_params


def _get_bytes_file_params_of_fields(fields: Sequence[ModelField]) -> list[tuple[str, bool]]:
    """Return `(alias, is_sequence)` of the `bytes` file params among the fields, in their order."""
    bytes_file_params: list[tuple[str, bool]] = []
    for field in fields:
        if isinstance(field.field_info, params.File):
            annotation = field.field_info.annotation
            if is_bytes_or_nonable_bytes_annotation(annotation):
                bytes_file_params.append((get_validation_alias(field), False))
            elif is_bytes_sequence_annotation(annotation):
                bytes_file_params.append((get_validation_alias(field), True))
    return bytes_file_params


def _get_form_values(form: FormData, key: str, *, is_sequence: bool) -> list[Union[UploadFile, str, None]]:
    """Return the values that FastAPI picks from the form for a `bytes` (or a sequence of `bytes`) file param."""
    return list(form.getlist(key)) if is_sequence else [form.get(key)]


async def _rewind_uploads_read_by_fastapi(
    form: list[tuple[str, Union[UploadFile, str]]], uploads: _UploadPositions
) -> None:
    """Undo FastAPI's reading of the uploaded files that the head will get exactly where FastAPI left them.

    We only rewind a file if the head's param will pick it from the same key again, if its stream belongs to
    this one upload in the form and appears there only once, and if the stream is still at the same position.
    Files that migrations replaced, shadowed, reused, moved under other keys, closed, seeked, swapped for other
    streams, or whose streams they shared with other uploads are passed on exactly as the migrations left them.
    """
    # A single pass over the form, so that forms with many files take linear time
    uploads_per_stream: Counter[int] = Counter()
    last_value_ids: dict[str, int] = {}
    value_ids: defaultdict[str, set[int]] = defaultdict(set)
    for key, value in form:
        if isinstance(value, UploadFile):
            uploads_per_stream[id(value.file)] += 1
        last_value_ids[key] = id(value)
        value_ids[key].add(id(value))
    for read in uploads.values():
        if read.is_sequence:
            is_picked = id(read.upload) in value_ids[read.form_key]
        else:
            # Like FastAPI, the head takes the last value of the key
            is_picked = last_value_ids.get(read.form_key) == id(read.upload)
        if (
            is_picked
            and read.upload.file is read.file
            and uploads_per_stream[id(read.file)] == 1
            and not read.file.closed
            and read.file.tell() == read.position
        ):
            await read.upload.seek(0)


_UPLOADS_TO_CLOSE_SCOPE_KEY = "cadwyn.uploads_to_close"
# How long we wait for each upload to close, so that an upload that never closes can't keep the request running forever
_UPLOAD_CLOSE_TIMEOUT_SECONDS = 3


class _UploadsToClose:
    """The uploads of a request that Cadwyn closes once the request is done, even if it gets cancelled.

    FastAPI closes the uploads of the form that it has parsed, but it doesn't shield closing them. So when the
    request gets cancelled, e.g. by a `BaseHTTPMiddleware` (like Cadwyn's) after the client has disconnected,
    the uploaded files stay open. FastAPI also doesn't know about the uploads that migrations put into the form,
    including the ones that later migrations replace or remove.
    """

    __slots__ = ("_uploads",)

    def __init__(self) -> None:
        super().__init__()
        # Keyed by id, so that we close every upload once. The values keep the ids valid
        self._uploads: dict[int, UploadFile] = {}

    def add(self, form: Iterable[tuple[str, Union[UploadFile, str]]]) -> None:
        for _, value in form:
            if isinstance(value, UploadFile):
                self._uploads.setdefault(id(value), value)

    async def close(self) -> None:
        # Background tasks and streams have finished by now, so nothing reads the uploads anymore. If FastAPI closes
        # its uploads after us, closing them again does nothing
        for upload in self._uploads.values():
            if getattr(upload.file, "closed", False):
                continue
            with anyio.move_on_after(_UPLOAD_CLOSE_TIMEOUT_SECONDS, shield=True) as scope:
                await upload.close()
            if scope.cancelled_caught:
                _logger.warning(
                    "Cadwyn couldn't close the upload %r within %s seconds after the request, so it may stay open",
                    upload.filename,
                    _UPLOAD_CLOSE_TIMEOUT_SECONDS,
                )


def _get_uploads_to_close(request: FastapiRequest) -> _UploadsToClose:
    uploads: Union[_UploadsToClose, None] = request.scope.get(_UPLOADS_TO_CLOSE_SCOPE_KEY)
    if uploads is None:
        # Outside of a Cadwyn app (e.g. with the routers of `generate_versioned_routers()`), we close them with the
        # stack that FastAPI closes its forms with
        uploads = _UploadsToClose()
        request.scope[_UPLOADS_TO_CLOSE_SCOPE_KEY] = uploads
        request.scope["fastapi_middleware_astack"].push_async_callback(uploads.close)
    return uploads
