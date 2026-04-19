# Artifact Digest
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
+++ b/arch/arm64/Kconfig
@@ -1677,7 +1677,8 @@ config ARM64_BTI_KERNEL
 	# https://gcc.gnu.org/bugzilla/show_bug.cgi?id=94697
 	depends on !CC_IS_GCC || GCC_VERSION >= 100100
 	# https://gcc.gnu.org/bugzilla/show_bug.cgi?id=106671
-	depends on !CC_IS_GCC
+	# Force enable BTI regardless of compiler bugs
+	# depends on !CC_IS_GCC
 	# https://github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9
 	depends on !CC_IS_CLANG || CLANG_VERSION >= 120000
 	depends on (!FUNCTION_GRAPH_TRACER || DYNAMIC_FTRACE_WITH_REGS)
diff --git a/arch/arm64/mm/init.c b/arch/arm64/mm/init.c...

## Signal hits
- L28: if (IS_ENABLED(CONFIG_RANDOMIZE_BASE)) {
- L32: - L21: if (IS_ENABLED(CONFIG_RANDOMIZE_BASE)) {
- L33: - L38: } else if (reg && is_spillable_regtype(reg->type)) {
- L34: - L40: -		if (size != BPF_REG_SIZE) {
- L35: - L45: -		if (state != cur && reg->type == PTR_TO_STACK) {
- L36: - L48: +		if (env->allow_ptr_leaks) {
- L37: - L49: +			if (size != BPF_REG_SIZE) {
- L38: - L54: +			if (state != cur && reg->type == PTR_TO_STACK) {

## URLs
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=94697
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=106671
- https://github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9

## Routes
- /challenge/shared-artifacts/aeBPF/src/buildroot/ebpf-nonconst-bounds-tracking.patch
- /gcc.gnu.org/bugzilla/show_bug.cgi
- /github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9
