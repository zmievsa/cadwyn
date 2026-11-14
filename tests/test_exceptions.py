import pickle

from cadwyn.exceptions import CadwynHeadRequestValidationError


def test__pickle__of_cadwyn_head_request_validation_error__should_restore_the_error():
    error = CadwynHeadRequestValidationError([{"type": "missing", "loc": ("a",)}], body={"b": 1}, version="2000-01-01")

    restored = pickle.loads(pickle.dumps(error))

    assert type(restored) is CadwynHeadRequestValidationError
    assert (restored.errors, restored.body, restored.version, str(restored)) == (
        error.errors,
        error.body,
        error.version,
        str(error),
    )
