cmake_minimum_required(VERSION 3.13)

project(os_videosdr_ps_shim C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_executable(ps_shim
    src/main.c
    src/ring_backend.c
)

# B.3 board sender. It is a separate executable because it uses the
# DDRRingWriter MMIO/ring contract, not the synthetic descriptor-ring demo.
add_executable(tx_shim
    src/tx_shim.c
)

target_include_directories(ps_shim PRIVATE include)
target_include_directories(tx_shim PRIVATE include)

if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(ps_shim PRIVATE -Wall -Wextra -Wpedantic)
    target_compile_options(tx_shim PRIVATE -Wall -Wextra -Wpedantic)
endif()
