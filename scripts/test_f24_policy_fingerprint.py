from scripts.final_master_qc import qc_policy_fingerprint


def test_policy_fingerprint_is_sha256_width() -> None:
    assert len(qc_policy_fingerprint()) == 64
