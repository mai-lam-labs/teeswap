import dataclasses
from pathlib import Path

import pytest

from teeswap.attestation import AttestationError, VaportpmParseError, _parse_vaportpm_output

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_nitro_fixture() -> None:
    raw = (FIXTURES / "test-nitro-fixture.json").read_text()
    output = _parse_vaportpm_output(raw)

    assert output.nonce == "230af3f7c0ec43ccf99a4cab47ac61469a36ea74b1e79740fdf8ccfc8f56161a"
    assert "sha384" in output.pcrs
    assert len(output.pcrs["sha384"]) == 24
    assert "ecc_p256" in output.ak_pubkeys
    assert output.ak_pubkeys["ecc_p256"].x.startswith("7dd307a7")
    assert "ecc_p256" in output.attestation.tpm
    assert output.attestation.tpm["ecc_p256"].attest_data.startswith("ff54434780")
    assert output.attestation.nitro is not None
    assert output.attestation.nitro.document.startswith("8444a101")
    assert output.attestation.gcp is None


def test_parse_gcp_fixture() -> None:
    raw = (FIXTURES / "test-gcp-amd-fixture.json").read_text()
    output = _parse_vaportpm_output(raw)

    assert "sha256" in output.pcrs
    assert "ecc_p256" in output.ak_pubkeys
    assert output.attestation.gcp is not None
    assert output.attestation.gcp.ak_cert_chain.startswith("-----BEGIN CERTIFICATE-----")
    assert output.attestation.nitro is None


def test_parsed_output_is_frozen() -> None:
    raw = (FIXTURES / "test-nitro-fixture.json").read_text()
    output = _parse_vaportpm_output(raw)

    replaced = dataclasses.replace(output, nonce="replaced_nonce")
    assert replaced.nonce == "replaced_nonce"
    assert output.nonce != "replaced_nonce"


def test_parse_invalid_json_raises_parse_error() -> None:
    with pytest.raises(VaportpmParseError, match="failed to parse"):
        _parse_vaportpm_output("not json")


def test_parse_missing_field_raises_parse_error() -> None:
    with pytest.raises(VaportpmParseError, match="failed to parse"):
        _parse_vaportpm_output('{"nonce": "abc"}')


def test_parse_error_inherits_from_attestation_error() -> None:
    with pytest.raises(AttestationError):
        _parse_vaportpm_output("{}")
