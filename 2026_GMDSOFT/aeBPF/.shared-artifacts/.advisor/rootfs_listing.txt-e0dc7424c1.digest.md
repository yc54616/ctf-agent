# Artifact Digest
- artifact: /challenge/shared-artifacts/tmp/rootfs_listing.txt
- file_size: 5936
- file_type: text-like
- mode: text-scan-v1

## Head sample
- .
linuxrc
etc
etc/services
etc/protocols
etc/resolv.conf
etc/os-release
etc/hosts
etc/nsswitch.conf
etc/mdev.conf
etc/passwd
etc/init.d
etc/init.d/S01syslogd
etc/init.d/S02sysctl
etc/init.d/rcS
etc/init.d/S02klogd
etc/init.d/rcK
etc/init.d/S20urandom
etc/mtab
etc/profile
etc/issue
etc/shadow
etc/hostname
etc/fstab

## Tail sample
- usr/bin/unxz
usr/bin/sha1sum
usr/bin/w
usr/bin/id
usr/bin/microcom
usr/bin/killall
usr/bin/dos2unix
usr/bin/hostid
usr/bin/tail
usr/bin/last
usr/bin/lsof
usr/bin/bzcat
usr/bin/ts
usr/bin/sha256sum
usr/bin/readlink
usr/bin/paste
usr/bin/od
usr/bin/tftp
usr/bin/chvt
usr/bin/[
usr/bin/renice
usr/bin/expr
usr/bin/nproc

## Signal hits
- L107: usr/sbin/nologin
- L130: usr/bin/[[
- L149: usr/bin/traceroute
- L238: usr/bin/[
- L292: sbin/sulogin
- L298: sbin/route
- L302: sbin/iproute
- L335: bin/login
