# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/aeBPF_preview.txt-de7322ea9e.digest.md
- file_size: 2009
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF_preview.txt
- file_size: 5988
- file_type: text-like
- mode: text-scan-v1

## Head sample
- ===== build.sh =====
#!/bin/bash

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

## Signal hits
- L11: # if [ -f ../flag ]; then
- L12: #     cp ../flag buildroot/flag
- L13: #     echo "Injected flag file into Docker build context."
- L15: #     echo "DH{fake_flag}" > buildroot/flag
- L16: #     echo "Injected fake flag into Docker build context."
- L36: +		if (env->allow_ptr_leaks) {
- L37: +			if (size != BPF_REG_SIZE) {
- L42: +			if (state != cur && reg->type == PTR_TO_STACK) {

## URLs
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=94697
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=106671
- https://github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9
- https://buildroot.org/downloads/buildroot-2022.11.tar.gz

## Routes
- /challenge/shared-artifacts/aeBPF_preview.txt
- /bin/bash
- /flag
- /gcc.gnu.org/bugzilla/show_bug.cgi
- /github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9
- /buildroot.org/downloads/buildroot-2022.11.tar.gz
- /output/Image.gz
- /output
