cmake_minimum_required(VERSION 3.13)

project(os_videosdr_ps_shim C)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)

add_executable(ps_shim
    src/main.c
    src/ring_backend.c
)

target_include_directories(ps_shim PRIVATE include)

if(CMAKE_C_COMPILER_ID MATCHES "GNU|Clang")
    target_compile_options(ps_shim PRIVATE -Wall -Wextra -Wpedantic)
endif()
