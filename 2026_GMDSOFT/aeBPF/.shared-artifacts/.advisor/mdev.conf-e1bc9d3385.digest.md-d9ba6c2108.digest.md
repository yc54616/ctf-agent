# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/mdev.conf-e1bc9d3385.digest.md
- file_size: 1424
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/src/buildroot/mdev.conf
- file_size: 1002
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Provide user, group, and mode information for devices.  If a regex matches
# the device name provided by sysfs, use the appropriate user:group and mode
# instead of the default 0:0 660.
#
# Syntax:
# [-]devicename_regex user:group mode [=path]|[>path]|[!] [@|$|*cmd args...]
# [-]$ENVVAR=regex    user:group mode [=path]|[>path]|[!] [@|$|*cmd args...]
# [-]@maj,min[-min2]  user:group mode [=path]|[>path]|[!] [@|$|*cmd args...]
#
# [-]: do not stop on this match, continue reading mdev.conf
# =: move, >: move and create a symlink
# !: do not create device node
# @|$|*: run@cmd if $ACTION=add,  $cmd if $ACTION=remove, *cmd in all cases

null        0:0 666
zero        0:0 666
random      0:0 444

## Signal hits
- L13: # [-]devicename_regex user:group mode [=path]|[>path]|[!] [@|$|*cmd args...]
- L14: # [-]$ENVVAR=regex    user:group mode [=path]|[>path]|[!] [@|$|*cmd args...]
- L15: # [-]@maj,min[-min2]  user:group mode [=path]|[>path]|[!] [@|$|*cmd args...]
- L17: # [-]: do not stop on this match, continue reading mdev.conf
- L34: - L6: # [-]devicename_regex user:group mode [=path]|[>path]|[!] [@|$|*cmd args...]
- L35: - L7: # [-]$ENVVAR=regex    user:group mode [=path]|[>path]|[!] [@|$|*cmd args...]
- L36: - L8: # [-]@maj,min[-min2]  user:group mode [=path]|[>path]|[!] [@|$|*cmd args...]
- L37: - L10: # [-]: do not stop on this match, continue reading mdev.conf

## Routes
- /challenge/shared-artifacts/aeBPF/src/buildroot/mdev.conf
