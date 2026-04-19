# Artifact Digest
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
diff --git a/arch/arm64/mm/init.c b/arch/arm64/mm/init.c
index 3b269c7..dc159a5 100644
--- a/arch/arm64/mm/init.c
+++ b/arch/arm64/mm/init.c
@@ -308,11 +308,8 @@ void __init arm64_memblock_init(void)

 	if (IS_ENABLED(CONFIG_RANDOMIZE_BASE)) {
 		exte...

## Signal hits
- L21: if (IS_ENABLED(CONFIG_RANDOMIZE_BASE)) {
- L38: } else if (reg && is_spillable_regtype(reg->type)) {
- L40: -		if (size != BPF_REG_SIZE) {
- L45: -		if (state != cur && reg->type == PTR_TO_STACK) {
- L48: +		if (env->allow_ptr_leaks) {
- L49: +			if (size != BPF_REG_SIZE) {
- L54: +			if (state != cur && reg->type == PTR_TO_STACK) {
- L59: +		} else {

## URLs
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=94697
- https://gcc.gnu.org/bugzilla/show_bug.cgi?id=106671
- https://github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9

## Routes
- /gcc.gnu.org/bugzilla/show_bug.cgi
- /github.com/llvm/llvm-project/commit/a88c722e687e6780dcd6a58718350dc76fcc4cc9
