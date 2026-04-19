# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/build_script.sh-55c9654a64.digest.md-0b1fae5900.digest.md
- file_size: 1103
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/build_script.sh-55c9654a64.digest.md
- file_size: 953
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/src/buildroot/build_script.sh
- file_size: 608
- file_type: text-like
- mode: text-scan-v1

## Head sample
- #!/bin/bash

BUILDROOT=$(pwd)

# Build kernel & rootfs
make

# Copy kernel image into /output
mkdir -p /output
cp $BUILDROOT/output/images/Image.gz /output/Image.gz

## Signal hits
- L34: - L28: # Create rootfs with fake flag (public)
- L35: - L29: echo 'DH{fake_flag}' > flag
- L36: - L30: chmod 400 flag
- L37: - L34: - L21: # Create rootfs with fake flag (public)
- L38: - L35: - L22: echo 'DH{fake_flag}' > flag
- L39: - L36: - L23: chmod 400 flag
- L40: - L38: ## Routes
- L42: ## Routes

## Routes
- /challenge/shared-artifacts/.advisor/build_script.sh-55c9654a64.digest.md
- /challenge/shared-artifacts/aeBPF/src/buildroot/build_script.sh
- /bin/bash
- /output
- /output/Image.gz
- /rootfs
- /init
- /mdev.conf
