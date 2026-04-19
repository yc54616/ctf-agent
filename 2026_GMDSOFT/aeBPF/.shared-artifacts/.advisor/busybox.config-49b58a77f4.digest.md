# Artifact Digest
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
# CONFIG_FEATURE_COMPRESS_USAGE is not set
CONFIG_LFS=y
# CONFIG_PAM is not set
CONFIG_FEATURE_DEVPTS=y
CONFIG_FEATURE_UTMP=y
CONFIG_FEATURE_WTMP=y
# CONFIG_FEATURE_PIDFILE is not set

## Middle sample
- # CONFIG_FEATURE_SUN_LABEL is not set
# CONFIG_FEATURE_OSF_LABEL is not set
CONFIG_FEATURE_GPT_LABEL=y
CONFIG_FEATURE_FDISK_ADVANCED=y
# CONFIG_FINDFS is not set
CONFIG_FLOCK=y
CONFIG_FDFLUSH=y
CONFIG_FREERAMDISK=y
# CONFIG_FSCK_MINIX is not set
CONFIG_FSFREEZE=y
CONFIG_FSTRIM=y
CONFIG_GETOPT=y
CONFIG_FEATURE_GETOPT_LONG=y
CONFIG_HEXDUMP=y
# CONFIG_HD is not set
CONFIG_XXD=y
CONFIG_HWCLOCK=y
CONFIG_FEATURE_HWCLOCK_ADJTIME_FHS=y
# CONFIG_IONICE is not set
CONFIG_IPCRM=y
CONFIG_IPCS=y
CONFIG_LA...

## Tail sample
- SH is not set
# CONFIG_HUSH_BASH_COMPAT is not set
# CONFIG_HUSH_BRACE_EXPANSION is not set
# CONFIG_HUSH_BASH_SOURCE_CURDIR is not set
# CONFIG_HUSH_LINENO_VAR is not set
# CONFIG_HUSH_INTERACTIVE is not set
# CONFIG_HUSH_SAVEHISTORY is not set
# CONFIG_HUSH_JOB is not set
# CONFIG_HUSH_TICK is not set
# CONFIG_HUSH_IF is not set
# CONFIG_HUSH_LOOPS is not set
# CONFIG_HUSH_CASE is not set
# CONFIG_HUSH_FUNCTIONS is not set
# CONFIG_HUSH_LOCAL is not set
# CONFIG_HUSH_RANDOM_SUPPORT is not s...

## Signal hits
- L52: CONFIG_EXTRA_CFLAGS=""
- L53: CONFIG_EXTRA_LDFLAGS=""
- L517: # Login/Password Management Utilities
- L541: CONFIG_LOGIN=y
- L542: # CONFIG_LOGIN_SESSION_AS_CHILD is not set
- L543: # CONFIG_LOGIN_SCRIPTS is not set
- L544: CONFIG_FEATURE_NOLOGIN=y
- L552: CONFIG_SULOGIN=y

## Routes
- /proc/self/exe
- /_install
- /lib/modules
- /var/spool/cron
- /var/run/ifstate
- /etc/iproute2
- /usr/share/udhcpc/default.script
