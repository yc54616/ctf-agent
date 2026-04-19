from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_sandbox_dockerfile_targets_lane_usable_headless_tooling() -> None:
    dockerfile = (REPO_ROOT / "sandbox" / "Dockerfile.sandbox").read_text(encoding="utf-8")
    dockerfile_lower = dockerfile.lower()

    assert "FROM ubuntu:24.04" in dockerfile
    assert "ARG UBUNTU_MIRROR=http://mirror.navercorp.com/ubuntu" in dockerfile
    assert "archive.ubuntu.com/ubuntu" in dockerfile
    assert "security.ubuntu.com/ubuntu" in dockerfile
    assert "PIPX_BIN_DIR=/usr/local/bin" in dockerfile
    assert "openjdk-21-jdk-headless" in dockerfile
    assert "linux-tools-common" in dockerfile
    assert "bpftool" in dockerfile
    assert "bpftrace" in dockerfile
    assert "pipx install certipy-ad" in dockerfile
    assert "pipx install awscli" in dockerfile
    assert 'pipx install "git+https://github.com/Pennyw0rth/NetExec"' in dockerfile
    assert 'pipx install "git+https://github.com/cddmp/enum4linux-ng"' in dockerfile
    assert "pipx install jefferson" in dockerfile
    assert "pipx install ubi-reader" in dockerfile
    assert "pipx install unblob" in dockerfile
    assert "pipx install solc-select" in dockerfile
    assert "pipx install vyper" in dockerfile
    assert "pipx install semgrep" in dockerfile
    assert "uv tool install slither-analyzer" in dockerfile
    assert "foundryup" in dockerfile
    assert "gem install evil-winrm" in dockerfile
    assert "pwndbg" in dockerfile
    assert "ghidra-headless" in dockerfile
    assert "azure-cli" in dockerfile
    assert "google-cloud-cli-linux-" in dockerfile
    assert "httpx" in dockerfile
    assert "subfinder" in dockerfile
    assert "dnsx" in dockerfile
    assert "naabu" in dockerfile
    assert "katana" in dockerfile
    assert "interactsh-client" in dockerfile
    assert "amass" in dockerfile
    assert "kerbrute" in dockerfile
    assert "smali" in dockerfile
    assert "baksmali" in dockerfile
    assert "dex2jar" in dockerfile
    assert "Miniforge3" in dockerfile
    assert "sage" in dockerfile
    assert "/opt/wordlists/seclists" in dockerfile
    assert "/opt/wordlists/assetnote" in dockerfile
    assert 'amd64) \\' in dockerfile
    assert 'ffuf_arch="amd64"' in dockerfile
    assert 'gobuster_arch="x86_64"' in dockerfile
    assert 'nuclei_arch="amd64"' in dockerfile
    assert 'ferox_arch="x86_64"' in dockerfile
    assert 'kubectl_arch="amd64"' in dockerfile
    assert 'helm_arch="amd64"' in dockerfile
    assert 'pd_arch="amd64"' in dockerfile
    assert 'gcloud_arch="x86_64"' in dockerfile
    assert 'amass_arch="amd64"' in dockerfile

    for forbidden in ("novnc", "x11vnc", "xvfb", "cutter", "burpsuite", "ghidrarun"):
        assert forbidden not in dockerfile_lower


def test_sandbox_tools_manifest_mentions_lane_usable_scope_and_major_domains() -> None:
    manifest = (REPO_ROOT / "sandbox" / "sandbox-tools.txt").read_text(encoding="utf-8")

    for needle in (
        "Lane-usable only",
        "ghidra-headless",
        "pwndbg",
        "bpftool",
        "bpftrace",
        "httpx",
        "subfinder",
        "dnsx",
        "naabu",
        "katana",
        "interactsh-client",
        "amass",
        "impacket-*",
        "nxc",
        "certipy",
        "evil-winrm",
        "enum4linux-ng",
        "kerbrute",
        "smali",
        "baksmali",
        "dex2jar",
        "frida-ps",
        "jefferson",
        "ubireader_*",
        "unblob",
        "openocd",
        "solc-select",
        "vyper",
        "sage",
        "forge",
        "slither",
        "nuclei",
        "feroxbuster",
        "/opt/wordlists/seclists",
        "/opt/wordlists/assetnote",
    ):
        assert needle in manifest


def test_sandbox_smoke_check_covers_new_headless_contract() -> None:
    smoke = (REPO_ROOT / "sandbox" / "smoke-check.sh").read_text(encoding="utf-8")

    assert "set -euo pipefail" in smoke
    for needle in (
        "ghidra-headless",
        "bpftool",
        "bpftrace",
        "httpx -version",
        "subfinder -version",
        "dnsx -version",
        "naabu -version",
        "katana -version",
        "interactsh-client -h",
        "amass --help",
        "certipy -h",
        "evil-winrm -h",
        "enum4linux-ng -h",
        "kerbrute --help",
        "smali -h",
        "baksmali -h",
        "dex2jar --help",
        "frida-ps -h",
        "jefferson --help",
        "ubireader_extract_images -h",
        "unblob --help",
        "openocd --version",
        "solc-select --help",
        "vyper --version",
        "sage --version",
        "pipx list",
        "forge --version",
        "cast --version",
        "slither --version",
        "semgrep --version",
        "nuclei -version",
        "feroxbuster --help",
        "az version",
        "gcloud --version",
        "/opt/wordlists/seclists",
        "/opt/wordlists/assetnote",
        "pi import pwndbg",
    ):
        assert needle in smoke
