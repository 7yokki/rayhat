cmake_minimum_required(VERSION 3.10)
project(sample_threadx C)

set(CMAKE_C_STANDARD 99)
find_package(Threads REQUIRED)

set(THREADX_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/../../../../")

include_directories(
    "${THREADX_ROOT}/common/inc"
    "${THREADX_ROOT}/ports/linux/gnu/inc"
)

# Sadece bizim ana kodumuzu derliyoruz, gerisi kütüphaneden gelecek
add_executable(sample_threadx sample_threadx.c)

target_link_libraries(sample_threadx 
    PRIVATE 
    "${THREADX_ROOT}/build/libthreadx.a"
    Threads::Threads
    m
)
