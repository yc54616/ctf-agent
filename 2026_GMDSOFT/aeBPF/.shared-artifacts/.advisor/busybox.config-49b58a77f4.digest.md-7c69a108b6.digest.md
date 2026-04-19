# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/busybox.config-49b58a77f4.digest.md
- file_size: 2171
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/src/buildroot/configs/busybox.config
- file_size: 32424
- file_type: text-like
- mode: text-scan-v1

## Head sample
- #
# Automatically generated make config: don't edit
# Busybox version: 1.35.0
# Wed Jan  4 12:02:17 2023
#
CONFIG_HAVE_DOT_CONFIG=y

#
# Settings
#
CONFIG_DESKTOP=y
# CONFIG_EXTRA_COMPAT is not set
# CONFIG_FEDORA_COMPAT is not set
CONFIG_INCLUDE_SUSv2=y
CONFIG_LONG_OPTS=y
CONFIG_SHOW_USAGE=y
CONFIG_FEATURE_VERBOSE_USAGE=y

## Signal hits
- L75: - L52: CONFIG_EXTRA_CFLAGS=""
- L76: - L53: CONFIG_EXTRA_LDFLAGS=""
- L77: - L517: # Login/Password Management Utilities
- L78: - L541: CONFIG_LOGIN=y
- L79: - L542: # CONFIG_LOGIN_SESSION_AS_CHILD is not set
- L80: - L543: # CONFIG_LOGIN_SCRIPTS is not set
- L81: - L544: CONFIG_FEATURE_NOLOGIN=y
- L82: - L552: CONFIG_SULOGIN=y

## Routes
- /challenge/shared-artifacts/aeBPF/src/buildroot/configs/busybox.config
- /proc/self/exe
- /_install
- /lib/modules
- /var/spool/cron
- /var/run/ifstate
- /etc/iproute2
- /usr/share/udhcpc/default.script
