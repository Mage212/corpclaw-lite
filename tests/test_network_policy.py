from corpclaw_lite.security.network_policy import NetworkPolicy


def test_network_policy():
    """NetworkPolicy returns deny-all networking with no environment variables."""
    policy = NetworkPolicy()
    args = policy.to_docker_args()
    assert args["network_mode"] == "none"
    assert "environment" not in args
