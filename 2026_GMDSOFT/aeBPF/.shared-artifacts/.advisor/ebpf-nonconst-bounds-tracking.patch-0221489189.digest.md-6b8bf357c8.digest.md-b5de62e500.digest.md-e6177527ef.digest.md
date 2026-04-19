# Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/ebpf-nonconst-bounds-tracking.patch-0221489189.digest.md-6b8bf357c8.digest.md-b5de62e500.digest.md
- file_size: 2059
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/ebpf-nonconst-bounds-tracking.patch-0221489189.digest.md-6b8bf357c8.digest.md
- file_size: 1976
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/.advisor/ebpf-nonconst-bounds-tracking.patch-0221489189.digest.md
- file_size: 1803
- file_type: text-like
- mode: text-scan-v1

## Head sample
- # Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/src/buildroot/ebpf-nonconst-bounds-tracking.patch
- file_size: 2958
- file_type: text-like
- mode: text-scan-v1

## Head sample
- diff --git a/arch/arm64/Kconfig b/arch/arm64/Kconfig
index 9d3cbe7..b611339 100644
--- a/arch/arm64/Kconfig

## Signal hits
- L34: - L32: - L28: if (IS_ENABLED(CONFIG_RANDOMIZE_BASE)) {
- L35: - L33: - L32: - L21: if (IS_ENABLED(CONFIG_RANDOMIZE_BASE)) {
- L36: - L34: - L33: - L38: } else if (reg && is_spillable_regtype(reg->type)) {
- L37: - L35: - L34: - L40: -		if (size != BPF_REG_SIZE) {
- L38: - L36: - L35: - L45: -		if (state != cur && reg->type == PTR_TO_STACK) {
- L39: - L37: - L36: - L48: +		if (env->allow_ptr_leaks) {
- L40: - L38: - L37: - L49: +			if (size != BPF_REG_SIZE) {
- L41: - L39: - L38: - L54: +			if (state != cur && reg->type == PTR_TO_STACK) {

## URLs
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=94697
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=106671
- https://github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9

## Routes
- /challenge/shared-artifacts/.advisor/ebpf-nonconst-bounds-tracking.patch-0221489189.digest.md-6b8bf357c8.digest.md
- /challenge/shared-artifacts/.advisor/ebpf-nonconst-bounds-tracking.patch-0221489189.digest.md
- /challenge/shared-artifacts/aeBPF/src/buildroot/ebpf-nonconst-bounds-tracking.patch
- /gcc.gnu.org/bugzilla/show_bug.cgi
- /github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9
