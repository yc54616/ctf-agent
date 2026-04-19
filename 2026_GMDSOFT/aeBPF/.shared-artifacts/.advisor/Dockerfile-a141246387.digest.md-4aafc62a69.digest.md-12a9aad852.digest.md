# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/Dockerfile-a141246387.digest.md-4aafc62a69.digest.md
- file_size: 1144
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/Dockerfile-a141246387.digest.md
- file_size: 999
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/chal/Dockerfile
- file_size: 868
- file_type: text-like
- mode: text-scan-v1

## Head sample
- FROM alpine:3.17@sha256:f271e74b17ced29b915d351685fd4644785c6d1559dd1f2d4189a5e851ef753a

ENV user aebpf
ENV port 31337

# Install dependencies w/ qemu version pinned
COPY deploy/qemu-system-aarch64-7.1.0-r7.apk /
RUN apk add --no-cache socat /qemu-system-aarch64-7.1.0-r7.apk \
 && rm qemu-system-aarch64-7.1.0-r7.apk

## Signal hits
- L33: - L27: deploy/rootfs.cpio.gz deploy/run.sh flag /home/$user/
- L34: - L34: - L20: deploy/rootfs.cpio.gz deploy/run.sh flag /home/$user/
- L35: - L35: - L26: && chmod 660 flag
- L36: - L37: ## Routes
- L38: ## Routes

## Routes
- /challenge/shared-artifacts/.advisor/Dockerfile-a141246387.digest.md
- /challenge/shared-artifacts/aeBPF/chal/Dockerfile
- /qemu-system-aarch64-7.1.0-r7.apk
- /tmp
- /var/tmp
- /dev/shm
- /home
- /run.sh
