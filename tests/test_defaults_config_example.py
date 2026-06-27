from importlib.resources import files

from pycastle.scaffold import _BundledDefaultsIntrospection


def test_rendered_config_example_includes_unknown_reset_duration_fields() -> None:
    example = _BundledDefaultsIntrospection.from_defaults(
        files("pycastle.defaults")
    ).render_config_example()

    assert "# Minimum unknown-reset duration (hours)" in example
    assert "claude_minimum_unknown_reset_duration_hours = 0.0" in example
    assert "codex_minimum_unknown_reset_duration_hours = 0.0" in example
    assert "opencode_minimum_unknown_reset_duration_hours = 1.0" in example
