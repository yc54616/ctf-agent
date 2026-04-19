# Artifact Digest
- artifact: /challenge/shared-artifacts/zip_list.txt
- file_size: 530
- file_type: text-like
- mode: text-scan-v1

## Head sample
- src/
src/build.sh
src/buildroot/
src/buildroot/init
src/buildroot/build_script.sh
src/buildroot/ebpf-nonconst-bounds-tracking.patch
src/buildroot/Dockerfile
src/buildroot/mdev.conf
src/buildroot/configs/
src/buildroot/configs/linux.config
src/buildroot/configs/buildroot.config
src/buildroot/configs/busybox.config
src/output/
src/output/Image.gz
src/output/rootfs.cpio.gz
chal/
chal/deploy/
chal/deploy/qemu-system-aarch64-7.1.0-r7.apk
chal/deploy/Image.gz
chal/deploy/run.sh
chal/deploy/rootfs.cpio.gz
chal/flag
chal/Dockerfile

## Signal hits
- L22: chal/flag
