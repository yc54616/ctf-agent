# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/Dockerfile-6fe5ca491b.digest.md-9c302aa020.digest.md
- file_size: 1478
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/Dockerfile-6fe5ca491b.digest.md
- file_size: 1435
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/src/buildroot/Dockerfile
- file_size: 1040
- file_type: text-like
- mode: text-scan-v1

## Head sample
- FROM ubuntu:22.04

RUN sed -i "s/http:\/\/archive.ubuntu.com/http:\/\/mirror.kakao.com/g" /etc/apt/sources.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive \
    apt-get install --no-install-recommends -y \
      sed make binutils build-essential diffutils gcc g++ \
      bash patch gzip bzip2 perl tar cpio unzip rsync file bc wget \
      python3 libncurses5-dev libelf-dev libssl-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

## Signal hits
- L33: - L29: ## Routes
- L38: ## Routes

## URLs
- https://buildroot.org/downloads/buildroot-2022.11.tar.gz

## Routes
- /challenge/shared-artifacts/.advisor/Dockerfile-6fe5ca491b.digest.md
- /challenge/shared-artifacts/aeBPF/src/buildroot/Dockerfile
- /archive.ubuntu.com/http:
- /mirror.kakao.com/g
- /etc/apt/sources.list
- /var/lib/apt/lists
- /buildroot.org/downloads/buildroot-2022.11.tar.gz
- /buildroot-2022.11/.config
