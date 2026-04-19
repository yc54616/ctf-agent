# Artifact Digest
- artifact: /challenge/shared-artifacts/aeBPF/src/buildroot/configs/buildroot.config
- file_size: 121842
- file_type: text-like
- mode: text-scan-v1

## Head sample
- #
# Automatically generated file; DO NOT EDIT.
# Buildroot 2022.11-612-g1381a4d288 Configuration
#
BR2_HAVE_DOT_CONFIG=y
BR2_HOST_GCC_AT_LEAST_4_9=y
BR2_HOST_GCC_AT_LEAST_5=y
BR2_HOST_GCC_AT_LEAST_6=y
BR2_HOST_GCC_AT_LEAST_7=y
BR2_HOST_GCC_AT_LEAST_8=y
BR2_HOST_GCC_AT_LEAST_9=y

#
# Target options
#
BR2_ARCH_IS_64=y
BR2_USE_MMU=y
# BR2_arcle is not set
# BR2_arceb is not set
# BR2_arm is not set
# BR2_armeb is not set
BR2_aarch64=y
# BR2_aarch64_be is not set
# BR2_i386 is not set

## Middle sample
- ++, threads
#
# BR2_PACKAGE_PAHO_MQTT_C is not set

#
# paho-mqtt-cpp needs a toolchain w/ threads, C++
#

#
# pistache needs a toolchain w/ C++, gcc >= 7, threads, wchar, not binutils bug 27597
#
# BR2_PACKAGE_QDECODER is not set

#
# qpid-proton needs a toolchain w/ C++, dynamic library, threads
#
# BR2_PACKAGE_RABBITMQ_C is not set

#
# resiprocate needs a toolchain w/ C++, threads, wchar
#

#
# restclient-cpp needs a toolchain w/ C++, gcc >= 4.8

## Tail sample
- et
# BR2_PACKAGE_MEDIA_CTL is not set
# BR2_PACKAGE_SCHIFRA is not set
# BR2_PACKAGE_ZXING is not set
# BR2_PACKAGE_BLACKBOX is not set
# BR2_KERNEL_HEADERS_3_0 is not set
# BR2_KERNEL_HEADERS_3_11 is not set
# BR2_KERNEL_HEADERS_3_13 is not set
# BR2_KERNEL_HEADERS_3_15 is not set
# BR2_PACKAGE_DIRECTFB_EXAMPLES_ANDI is not set
# BR2_PACKAGE_DIRECTFB_EXAMPLES_BLTLOAD is not set
# BR2_PACKAGE_DIRECTFB_EXAMPLES_CPULOAD is not set
# BR2_PACKAGE_DIRECTFB_EXAMPLES_DATABUFFER is not set
# BR2_PACK...

## Signal hits
- L276: BR2_TARGET_LDFLAGS=""
- L464: # BR2_TARGET_ENABLE_ROOT_LOGIN is not set
- L580: # BR2_PACKAGE_DVDAUTHOR is not set
- L1133: # apitrace needs a toolchain w/ C++, wchar, dynamic library, threads, gcc >= 7
- L1591: # BR2_PACKAGE_LIBKCAPI is not set
- L1918: # hidapi needs udev /dev management and a toolchain w/ NPTL, threads, gcc >= 4.9
- L1923: # lcdapi needs a toolchain w/ C++, threads
- L2072: # rapidjson needs a toolchain w/ C++

## URLs
- http://sources.buildroot.net
- https://cdn.kernel.org/pub
- http://ftpmirror.gnu.org
- http://rocks.moonscript.org
- https://cpan.metacpan.org
- https://cdn.kernel.org/pub/linux/kernel/v5.x/linux-5.15.94.tar.xz
- http://buildroot.org/manual.html#faq-no-binary-packages

## Routes
- /buildroot-2022.11/configs/qemu_aarch64_virt_defconfig
- /host
- /sources.buildroot.net
- /cdn.kernel.org/pub
- /ftpmirror.gnu.org
- /rocks.moonscript.org
- /cpan.metacpan.org
- /local.mk
