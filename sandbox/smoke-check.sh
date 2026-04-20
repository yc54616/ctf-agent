#!/usr/bin/env bash
set -euo pipefail

echo "[smoke] lane-usable command presence"
for cmd in \
  rg fdfind tree jq sqlite3 socat tcpdump tshark dig whois http wfuzz \
  httpx subfinder dnsx naabu katana interactsh-client amass \
  sqlmap whatweb nikto dirb hydra ffuf gobuster feroxbuster nuclei \
  gdb gdb-multiarch r2 qemu-x86_64-static qemu-system-x86_64 bpftool bpftrace \
  apktool jadx ghidra-headless skopeo kubectl helm \
  impacket-smbclient nxc certipy evil-winrm enum4linux-ng kerbrute smbclient rpcclient \
  smali baksmali dex2jar frida-ps objection \
  jefferson ubireader_extract_images unblob openocd avrdude srec_cat \
  forge cast anvil slither solc-select vyper sage semgrep hashcat yara cado-nfs \
  aws az gcloud
do
  command -v "$cmd" >/dev/null
done

echo "[smoke] representative version/help checks"
node -v >/dev/null
npm -v >/dev/null
codex --version >/dev/null
gemini --version >/dev/null
httpx -version >/dev/null
subfinder -version >/dev/null
dnsx -version >/dev/null
naabu -version >/dev/null
katana -version >/dev/null
interactsh-client -h >/dev/null
amass --help >/dev/null 2>&1
ffuf -V >/dev/null
gobuster version >/dev/null
feroxbuster --help >/dev/null
nuclei -version >/dev/null
kubectl version --client >/dev/null
helm version >/dev/null
jadx --help >/dev/null
ghidra-headless -help >/dev/null 2>&1
bpftool version >/dev/null
bpftrace --help >/dev/null
impacket-smbclient -h >/dev/null
nxc --help >/dev/null
certipy -h >/dev/null
evil-winrm -h >/dev/null
enum4linux-ng -h >/dev/null
kerbrute --help >/dev/null 2>&1
smali -h >/dev/null
baksmali -h >/dev/null
dex2jar --help >/dev/null 2>&1
frida-ps -h >/dev/null
objection --help >/dev/null
jefferson --help >/dev/null 2>&1
ubireader_extract_images -h >/dev/null 2>&1
unblob --help >/dev/null
openocd --version >/dev/null
avrdude -? >/dev/null 2>&1 || avrdude -h >/dev/null 2>&1
srec_cat --version >/dev/null 2>&1
forge --version >/dev/null
cast --version >/dev/null
anvil --version >/dev/null
slither --version >/dev/null
solc-select --help >/dev/null
vyper --version >/dev/null
sage --version >/dev/null
semgrep --version >/dev/null
hashcat --version >/dev/null
yara --version >/dev/null
aws --version >/dev/null 2>&1
az version >/dev/null
gcloud --version >/dev/null
gdb -q -ex 'pi import pwndbg' -ex 'quit' >/dev/null 2>&1
pipx list >/dev/null

echo "[smoke] bundled data"
[ -d /opt/wordlists/seclists ]
[ -d /opt/wordlists/assetnote ]

echo "[smoke] python import checks"
python3 - <<'PY'
import aiodocker
import angr
import bs4
import capstone
import claude_agent_sdk
import dnfile
import httpx
import keystone
import lxml
import oletools  # type: ignore
import pefile
import pydantic_ai
import pyroute2
import qiling
import scapy.all  # type: ignore
import z3
import zstandard
print("python imports ok")
PY

echo "[smoke] done"
