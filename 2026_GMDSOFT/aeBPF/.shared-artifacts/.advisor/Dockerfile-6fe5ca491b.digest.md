# Artifact Digest
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

RUN wget -qO- https://buildroot.org/downloads/buildroot-2022.11.tar.gz | tar xvz

COPY configs/buildroot.config               /buildroot-2022.11/.config
COPY configs/busybox.config                 /buildroot-2022.11/package/busybox/busybox.config
COPY configs/linux.config                   /buildroot-2022.11/board/qemu/aarch64-virt/linux.config
COPY ebpf-nonconst-bounds-tracking.patch    /buildroot-2022.11/ebpf-nonconst-bounds-tracking.patc...

## URLs
- https://buildroot.org/downloads/buildroot-2022.11.tar.gz

## Routes
- /archive.ubuntu.com/http:
- /mirror.kakao.com/g
- /etc/apt/sources.list
- /var/lib/apt/lists
- /buildroot.org/downloads/buildroot-2022.11.tar.gz
- /buildroot-2022.11/.config
- /buildroot-2022.11/package/busybox/busybox.config
- /buildroot-2022.11/board/qemu/aarch64-virt/linux.config
