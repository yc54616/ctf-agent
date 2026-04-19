# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/build.sh-01367debdb.digest.md
- file_size: 1148
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/src/build.sh
- file_size: 831
- file_type: text-like
- mode: text-scan-v1

## Head sample
- #!/bin/bash

# if [ -f ../flag ]; then
#     cp ../flag buildroot/flag
#     echo "Injected flag file into Docker build context."
# else
#     echo "DH{fake_flag}" > buildroot/flag
#     echo "Injected fake flag into Docker build context."
# fi

echo "Building Docker image..."

IMAGE_DIGEST=$(docker build -q buildroot)
CONTAINER_DIGEST=$(docker create $IMAGE_DIGEST)

echo "Image Digest:     $IMAGE_DIGEST"
echo "Container Digest: $CONTAINER_DIGEST"

## Signal hits
- L10: # if [ -f ../flag ]; then
- L11: #     cp ../flag buildroot/flag
- L12: #     echo "Injected flag file into Docker build context."
- L14: #     echo "DH{fake_flag}" > buildroot/flag
- L15: #     echo "Injected fake flag into Docker build context."
- L34: - L3: # if [ -f ../flag ]; then
- L35: - L4: #     cp ../flag buildroot/flag
- L36: - L5: #     echo "Injected flag file into Docker build context."

## Routes
- /challenge/shared-artifacts/aeBPF/src/build.sh
- /bin/bash
- /flag
- /output/Image.gz
- /output
- /output/rootfs.cpio.gz
