cmake_minimum_required(VERSION 3.13)

project(os_videosdr_ps_shim C CXX)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

add_executable(ps_shim
    src/main.c
    src/ring_backend.c
)

# B.3 board sender. It is a separate executable because it uses the
# DDRRingWriter MMIO/ring contract, not the synthetic descriptor-ring demo.
# It is C++ (not C) because it owns the ring/control buffers as native XRT
# buffer objects (xrt::bo), so the same physical memory the PL DMA writes
# into is the same, single, correctly-cacheable mapping this process reads
# from - see the file header comment in src/tx_shim.cpp for why a raw
# /dev/mem mapping of the ring was replaced.
add_executable(tx_shim
    src/tx_shim.cpp
)

target_include_directories(ps_shim PRIVATE include)
target_include_directories(tx_shim PRIVATE include /usr/include/xrt)
target_link_libraries(tx_shim PRIVATE xrt_coreutil uuid)

if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(ps_shim PRIVATE -Wall -Wextra -Wpedantic)
endif()
if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(tx_shim PRIVATE -Wall -Wextra)
endif()
