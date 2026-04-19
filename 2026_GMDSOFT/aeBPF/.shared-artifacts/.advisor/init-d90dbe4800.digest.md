# Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/src/buildroot/init
- file_size: 444
- file_type: text-like
- mode: text-scan-v1

## Head sample
- #!/bin/sh

echo -e "\nRunning init script...\n"

mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mdev -s
mkdir -p /dev/pts
mount -vt devpts -o gid=4,mode=620 none /dev/pts

echo 0 0 0 0 > /proc/sys/kernel/printk
echo 1 > /proc/sys/kernel/kptr_restrict
echo 1 > /proc/sys/kernel/dmesg_restrict

echo -e "\nBoot took $(cut -d' ' -f1 /proc/uptime) seconds\n"

setsid cttyhack setuidgid 1000 /bin/sh

poweroff -f

## Routes
- /bin/sh
- /proc
- /sys
- /dev
- /dev/pts
- /proc/sys/kernel/printk
- /proc/sys/kernel/kptr_restrict
- /proc/sys/kernel/dmesg_restrict
