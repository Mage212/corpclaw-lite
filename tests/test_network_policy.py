from pathlib import Path

from corpclaw_lite.security.network_policy import NetworkPolicy


def test_network_policy(tmp_path: Path):
    yaml_file = tmp_path / "network.yaml"
    yaml_file.write_text(
        "allowlist:\n"
        "  - external.api.com\n"
        "  - my-internal-service:8080\n"
    )
    
    policy = NetworkPolicy()
    policy.load_file(yaml_file)
    
    assert len(policy.allowlist) == 2
    assert "external.api.com" in policy.allowlist
    
    args = policy.to_docker_args()
    assert args["network_mode"] == "bridge"
    assert "ALLOWED_DOMAINS=external.api.com,my-internal-service:8080" in args["environment"]
