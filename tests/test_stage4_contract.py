from app.main import EXTERNAL_MODEL_CALLS_ENABLED
from app.providers.router import resolve_route


def test_external_model_calls_default_off():
    assert EXTERNAL_MODEL_CALLS_ENABLED is False


def test_supported_tasks_resolve_to_governed_route():
    route = resolve_route("copy")
    assert route.provider.value == "openai"
    assert route.model


def test_router_rejects_unknown_task():
    try:
        resolve_route("unknown")
    except ValueError as exc:
        assert str(exc).startswith("unsupported_task:")
    else:
        raise AssertionError("unknown tasks must fail closed")
