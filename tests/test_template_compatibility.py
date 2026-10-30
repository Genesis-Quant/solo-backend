from packaging.version import Version
import pytest

from core.apps.projects.templates import validate_template


@pytest.mark.parametrize("requirement", ["scheme>=1.12.0,<1.13.0", "scheme>=1.12.9,<1.13.0"])
def test_template_can_require_a_newer_patch_in_the_same_series(requirement):
    validate_template({"version": "1.12.4", "dependencies": [requirement]}, Version("1.12.9"))


def test_lookforward_template_is_not_offered_for_old_scheme():
    with pytest.raises(ValueError, match="不满足模板依赖"):
        validate_template({"version": "1.12.4", "dependencies": ["scheme>=1.12.9,<1.13.0"]}, Version("1.12.8"))


@pytest.mark.parametrize("requirement", [
    "scheme", "scheme>=1.11.0,<1.13.0", "scheme>=1.12.0", "scheme>=1.12.0,<1.14.0",
    "scheme>=1.12.0,<=1.13.0",
])
def test_template_rejects_unbounded_or_cross_series_requirements(requirement):
    with pytest.raises(ValueError):
        validate_template({"version": "1.12.4", "dependencies": [requirement]}, Version("1.12.9"))
