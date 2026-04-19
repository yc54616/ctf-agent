# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/linux.config-e1ae385363.digest.md
- file_size: 2238
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/src/buildroot/configs/linux.config
- file_size: 95850
- file_type: text-like
- mode: text-scan-v1

## Head sample
- #
# Automatically generated file; DO NOT EDIT.
# Linux/arm64 5.15.94 Kernel Configuration
#
CONFIG_CC_VERSION_TEXT="aarch64-buildroot-linux-gnu-gcc.br_real (Buildroot 2022.11-612-g1381a4d288) 11.3.0"
CONFIG_CC_IS_GCC=y
CONFIG_GCC_VERSION=110300
CONFIG_CLANG_VERSION=0
CONFIG_AS_IS_GNU=y
CONFIG_AS_VERSION=23800
CONFIG_LD_IS_BFD=y
CONFIG_LD_VERSION=23800
CONFIG_LLD_VERSION=0
CONFIG_CC_CAN_LINK=y
CONFIG_CC_CAN_LINK_STATIC=y
CONFIG_CC_HAS_ASM_GOTO=y
CONFIG_CC_HAS_ASM_GOTO_OUTPUT=y

## Signal hits
- L78: - L418: CONFIG_ARM64_PTR_AUTH=y
- L79: - L419: CONFIG_ARM64_PTR_AUTH_KERNEL=y
- L80: - L528: CONFIG_TRACE_IRQFLAGS_SUPPORT=y
- L81: - L529: CONFIG_TRACE_IRQFLAGS_NMI_SUPPORT=y
- L82: - L541: CONFIG_HAVE_REGS_AND_STACK_ACCESS_API=y
- L83: - L543: CONFIG_HAVE_FUNCTION_ARG_ACCESS_API=y
- L84: - L780: CONFIG_ARCH_USES_HIGH_VMA_FLAGS=y
- L85: - L822: CONFIG_IP_ADVANCED_ROUTER=y

## Routes
- /challenge/shared-artifacts/aeBPF/src/buildroot/configs/linux.config
- /sbin/modprobe
