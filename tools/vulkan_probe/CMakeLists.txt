cmake_minimum_required(VERSION 3.20)
project(rothbald_vulkan_probe LANGUAGES CXX)

find_package(Vulkan REQUIRED)

add_executable(rothbald-vulkan-probe main.cpp)
target_compile_features(rothbald-vulkan-probe PRIVATE cxx_std_17)
target_link_libraries(rothbald-vulkan-probe PRIVATE Vulkan::Vulkan)

if(MSVC)
  target_compile_options(rothbald-vulkan-probe PRIVATE /W4 /WX /utf-8)
endif()
