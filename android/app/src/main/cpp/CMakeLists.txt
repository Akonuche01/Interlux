cmake_minimum_required(VERSION 3.22.1)

project("interlux")

# Builds libinterlux.so: the native pseudo-terminal bridge.
#
# Uses forkpty()/openpty() from bionic, which is available to a normal
# untrusted_app process (verified by the feasibility test on this device
# family). No special permissions or SELinux exemptions are required.
add_library(interlux SHARED pty.c)

# NDK log library, for the __android_log_print calls in pty.c.
find_library(log-lib log)

target_link_libraries(interlux ${log-lib})
