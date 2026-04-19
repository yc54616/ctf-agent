# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/build.sh-01367debdb.digest.md-74eade6859.digest.md
- file_size: 1346
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
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

## Signal hits
- L17: # if [ -f ../flag ]; then
- L18: #     cp ../flag buildroot/flag
- L19: #     echo "Injected flag file into Docker build context."
- L21: #     echo "DH{fake_flag}" > buildroot/flag
- L22: #     echo "Injected fake flag into Docker build context."
- L34: - L10: # if [ -f ../flag ]; then
- L35: - L11: #     cp ../flag buildroot/flag
- L36: - L12: #     echo "Injected flag file into Docker build context."

## Routes
- /challenge/shared-artifacts/.advisor/build.sh-01367debdb.digest.md
- /challenge/shared-artifacts/aeBPF/src/build.sh
- /bin/bash
- /flag
- /output/Image.gz
- /output
- /output/rootfs.cpio.gz
