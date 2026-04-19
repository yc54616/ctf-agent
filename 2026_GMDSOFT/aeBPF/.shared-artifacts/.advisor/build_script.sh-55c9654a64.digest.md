# Artifact Digest
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

# Trim rootfs
mkdir /rootfs
cd /rootfs
gunzip -c $BUILDROOT/output/images/rootfs.cpio.gz | cpio -vid

# Copy init script & mdev.conf into rootfs
cp /init init && chmod 755 init
cp /mdev.conf etc/mdev.conf && chmod 644 etc/mdev.conf

# Create rootfs with fake flag (public)
echo 'DH{fake_flag}' > flag
chmod 400 flag
find . | cpio -H newc --owner root -o | gzip > /output/rootfs.cpio.gz

## Signal hits
- L21: # Create rootfs with fake flag (public)
- L22: echo 'DH{fake_flag}' > flag
- L23: chmod 400 flag

## Routes
- /bin/bash
- /output
- /output/Image.gz
- /rootfs
- /init
- /mdev.conf
- /output/rootfs.cpio.gz
