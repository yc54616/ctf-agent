# Artifact Digest
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
echo "Container Digest: $CONTAINER_DIGEST"

echo "Starting build..."
docker start -ai $CONTAINER_DIGEST

echo "Completed build. Copying files..."
mkdir -p output

## Tail sample
- he caller\n");
-			return -EINVAL;
+		if (env->allow_ptr_leaks) {
+			if (size != BPF_REG_SIZE) {
+				verbose_linfo(env, insn_idx, "; ");
+				verbose(env, "invalid size of register spill\n");
+				return -EACCES;
+			}
+			if (state != cur && reg->type == PTR_TO_STACK) {
+				verbose(env, "cannot spill pointers to stack into stack frame of the caller\n");
+				return -EINVAL;
+			}
+			save_register_state(state, spi, reg, size);
+		} else {
+			verbose(env, "pointer spill to stack is allowe...

## Signal hits
- L4: # if [ -f ../flag ]; then
- L5: #     cp ../flag buildroot/flag
- L6: #     echo "Injected flag file into Docker build context."
- L8: #     echo "DH{fake_flag}" > buildroot/flag
- L9: #     echo "Injected fake flag into Docker build context."
- L75: # Create rootfs with fake flag (public)
- L76: echo 'DH{fake_flag}' > flag
- L77: chmod 400 flag

## URLs
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=94697
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=106671
- https://github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9
- https://buildroot.org/downloads/buildroot-2022.11.tar.gz

## Routes
- /bin/bash
- /flag
- /output/Image.gz
- /output
- /output/rootfs.cpio.gz
- /bin/sh
- /proc
- /sys
