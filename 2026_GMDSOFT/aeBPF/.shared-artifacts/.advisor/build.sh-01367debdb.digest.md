# Artifact Digest
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

echo "Starting build..."
docker start -ai $CONTAINER_DIGEST

echo "Completed build. Copying files..."
mkdir -p output
docker cp $CONTAINER_DIGEST:/output/Image.gz                ./output/

## Signal hits
- L3: # if [ -f ../flag ]; then
- L4: #     cp ../flag buildroot/flag
- L5: #     echo "Injected flag file into Docker build context."
- L7: #     echo "DH{fake_flag}" > buildroot/flag
- L8: #     echo "Injected fake flag into Docker build context."

## Routes
- /bin/bash
- /flag
- /output/Image.gz
- /output
- /output/rootfs.cpio.gz
