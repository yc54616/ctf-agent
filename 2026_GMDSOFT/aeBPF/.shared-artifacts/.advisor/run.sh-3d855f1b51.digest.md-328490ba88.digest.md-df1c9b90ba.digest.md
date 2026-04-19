# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/run.sh-3d855f1b51.digest.md-328490ba88.digest.md
- file_size: 943
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/run.sh-3d855f1b51.digest.md
- file_size: 601
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/chal/deploy/run.sh
- file_size: 344
- file_type: text-like
- mode: text-scan-v1

## Head sample
- #!/bin/sh

echo -e 'Booting...\n'

exec timeout -s SIGKILL 300 qemu-system-aarch64 \
  -initrd ./rootfs.cpio.gz -kernel Image.gz \
  -M virt -cpu max -smp cores=1,threads=1 \
  -append "console=ttyAMA0 root=/dev/ram oops=panic panic=1 panic_on_warn=1 quiet" \
  -no-reboot -monitor /dev/null -net none -vga none -nographic -m 64M \
  -hda flag

## Signal hits
- L24: -hda flag
- L27: - L10: -hda flag
- L29: ## Routes
- L34: - L17: -hda flag
- L35: - L20: - L10: -hda flag
- L36: - L22: ## Routes
- L38: ## Routes

## Routes
- /challenge/shared-artifacts/.advisor/run.sh-3d855f1b51.digest.md
- /challenge/shared-artifacts/aeBPF/chal/deploy/run.sh
- /bin/sh
- /rootfs.cpio.gz
- /dev/ram
- /dev/null
