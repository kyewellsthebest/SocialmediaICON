from __future__ import annotations

import pytest

from worker.tasks.ingest import LicenseError, check_license


@pytest.mark.parametrize("tag", ["own", "licensed", "campaign", "permitted"])
def test_valid_licenses_pass_in_prod(tag):
    assert check_license(tag, env="prod") == tag


def test_license_none_is_refused_in_prod():
    with pytest.raises(LicenseError, match="license=none"):
        check_license("none", env="prod")


def test_license_none_is_allowed_in_dev_for_testing():
    assert check_license("none", env="dev") == "none"


def test_unknown_license_is_rejected_everywhere():
    with pytest.raises(LicenseError, match="unknown license"):
        check_license("probably-fine", env="dev")


def test_license_is_case_and_space_insensitive():
    assert check_license("  OWN  ", env="prod") == "own"
