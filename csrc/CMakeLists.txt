cmake_minimum_required(VERSION 3.20)
project(uring_slab_engine LANGUAGES CXX)

find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
find_package(pybind11 CONFIG REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(LIBURING REQUIRED IMPORTED_TARGET liburing)

pybind11_add_module(
  _uring_slab_engine
  data_engine.cpp
  pybind_module.cpp
)

target_compile_features(_uring_slab_engine PRIVATE cxx_std_20)
target_link_libraries(_uring_slab_engine PRIVATE PkgConfig::LIBURING)
target_compile_definitions(_uring_slab_engine PRIVATE _GNU_SOURCE)
target_compile_options(
  _uring_slab_engine
  PRIVATE
    -Wall
    -Wextra
    -Wpedantic
)
