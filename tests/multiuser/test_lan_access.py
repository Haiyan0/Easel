"""局域网入口的真实 PowerShell 配置行为。"""
from __future__ import annotations

import shutil
import subprocess
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "deploy/desktop/EaselLan.Common.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(not POWERSHELL, reason="需要 PowerShell")


def run_ps(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         f"$ErrorActionPreference='Stop'; . '{COMMON}'; {code}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def test_select_unique_private_address():
    result = run_ps("Resolve-EaselLanAddress -Addresses @('192.168.14.118')")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "192.168.14.118"


@pytest.mark.parametrize("addresses", ["@()", "@('192.168.1.2','10.0.0.2')"])
def test_ambiguous_or_missing_addresses_fail(addresses):
    result = run_ps(f"Resolve-EaselLanAddress -Addresses {addresses}")
    assert result.returncode != 0
    assert "lan_address_required" in result.stderr


@pytest.mark.parametrize("address", [
    "0.0.0.0", "127.0.0.1", "8.8.8.8", "198.18.0.1", "169.254.1.1",
    "192.168.1.2/path", "192.168.1.2:443", "192.168.001.2", "172.32.1.1",
])
def test_non_lan_addresses_fail(address):
    result = run_ps(f"Resolve-EaselLanAddress -Addresses @('{address}')")
    assert result.returncode != 0
    assert "invalid_lan_address" in result.stderr


@pytest.mark.parametrize("address", ["10.0.0.2", "172.16.0.2", "172.31.255.254"])
def test_other_private_ranges(address):
    result = run_ps(f"Resolve-EaselLanAddress -Addresses @('{address}')")
    assert result.returncode == 0, result.stderr


def test_proxy_preserves_origin_streaming_and_local_backend(tmp_path):
    storage = str(tmp_path / "CA storage").replace("\\", "/")
    result = run_ps(
        "New-EaselLanCaddyfile -Address '192.168.14.118' -HttpsPort 7861 "
        f"-BackendPort 7863 -AdminPort 17861 -StorageRoot '{storage}'"
    )
    assert result.returncode == 0, result.stderr
    text = result.stdout
    for expected in [
        "https://192.168.14.118:7861", "bind 192.168.14.118",
        "admin 127.0.0.1:17861", "tls internal", "skip_install_trust",
        "auto_https disable_redirects", "reverse_proxy 127.0.0.1:7863",
        "flush_interval -1", f'storage file_system "{storage}"',
    ]:
        assert expected in text
    assert "header_up" not in text


@pytest.mark.parametrize("https_port,backend,admin", [(7861, 7861, 17861), (7861, 7863, 7861), (0, 7863, 17861), (7861, 7863, 7863)])
def test_proxy_rejects_conflicting_or_invalid_ports(https_port, backend, admin):
    result = run_ps(
        "New-EaselLanCaddyfile -Address '192.168.14.118' "
        f"-HttpsPort {https_port} -BackendPort {backend} -AdminPort {admin} "
        "-StorageRoot 'C:/Easel/lan'"
    )
    assert result.returncode != 0


def test_proxy_rejects_config_injection_in_path():
    result = run_ps(
        "New-EaselLanCaddyfile -Address '192.168.14.118' "
        "-HttpsPort 7861 -BackendPort 7863 -AdminPort 17861 "
        "-StorageRoot ('C:/lan' + [char]10 + 'admin off')"
    )
    assert result.returncode != 0
    assert "invalid_storage_path" in result.stderr


@pytest.fixture
def certificates(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Easel test root")])
    paths = {}
    for kind, is_ca in [("root", True), ("leaf", False)]:
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
                .sign(key, hashes.SHA256()))
        path = tmp_path / f"{kind}.crt"
        path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        paths[kind] = path
    return paths


def import_certificate(path, *extra):
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
         str(ROOT / "deploy/desktop/Import-EaselLanCertificate.ps1"),
         "-CertificatePath", str(path), "-WhatIf", *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def test_root_certificate_can_be_previewed_without_import(certificates):
    result = import_certificate(certificates["root"])
    assert result.returncode == 0, result.stderr
    assert "certificate_thumbprint=" in result.stdout


def test_leaf_certificate_cannot_be_trusted_as_root(certificates):
    result = import_certificate(certificates["leaf"])
    assert result.returncode != 0
    assert "lan_certificate_not_ca" in result.stderr


def test_root_certificate_fingerprint_must_match(certificates):
    result = import_certificate(certificates["root"], "-ExpectedThumbprint", "00")
    assert result.returncode != 0
    assert "lan_certificate_thumbprint_mismatch" in result.stderr


def test_windows_powershell_can_parse_all_lan_scripts():
    engine = shutil.which("powershell.exe")
    if not engine:
        pytest.skip("需要 Windows PowerShell 5.1")
    result = subprocess.run(
        [engine, "-NoProfile", "-NonInteractive", "-Command",
         "$ErrorActionPreference='Stop'; "
         f"Get-ChildItem '{ROOT / 'deploy/desktop'}' -Filter '*Lan*.ps1' | ForEach-Object {{ "
         "$tokens=$null; $errors=$null; "
         "$null=[Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$errors); "
         "if ($errors.Count) { throw ($errors | Out-String) } }"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert result.returncode == 0, result.stderr


def test_windows_powershell_reads_chinese_lan_state(tmp_path):
    engine = shutil.which("powershell.exe")
    if not engine:
        pytest.skip("需要 Windows PowerShell 5.1")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"interface_alias": "以太网"}, ensure_ascii=False), encoding="utf-8")
    code = (f"$ErrorActionPreference='Stop'; . '{COMMON}'; "
            f"$s = Read-EaselLanJson -Path '{state}'; "
            "$expected = ([char]0x4ee5).ToString()+[char]0x592a+[char]0x7f51; "
            "if ($s.interface_alias -cne $expected) { throw 'unicode_state_corrupted' }")
    result = subprocess.run([engine, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", code],
                            capture_output=True, text=True, errors="replace")
    assert result.returncode == 0, result.stderr
