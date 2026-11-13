from pathlib import Path


def test_client_image_installs_active_remote_binary_and_documents_url() -> None:
    dockerfile = (
        Path(__file__).resolve().parents[2] / "client" / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "-o /apo-remote" in dockerfile
    assert "COPY --from=apobuild /apo-remote /usr/local/bin/apo-remote" in dockerfile
    assert "APO_MCP_URL=" in dockerfile
    assert "APO_SERVER_URL=" not in dockerfile
    assert "/usr/local/bin/apo\n" not in dockerfile
