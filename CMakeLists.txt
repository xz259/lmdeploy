# Copyright (c) 2019-2023, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

cmake_minimum_required(VERSION 3.11 FATAL_ERROR) # for PyTorch extensions, version should be greater than 3.13
project(TurboMind LANGUAGES CXX CUDA)

find_package(CUDA 10.2 REQUIRED)

if(${CUDA_VERSION_MAJOR} VERSION_GREATER_EQUAL "11")
  add_definitions("-DENABLE_BF16")
  message("CUDA_VERSION ${CUDA_VERSION_MAJOR}.${CUDA_VERSION_MINOR} is greater or equal than 11.0, enable -DENABLE_BF16 flag")
endif()

# if((${CUDA_VERSION_MAJOR} VERSION_GREATER_EQUAL "11" AND ${CUDA_VERSION_MINOR} VERSION_GREATER_EQUAL "8") OR (${CUDA_VERSION_MAJOR} VERSION_GREATER_EQUAL "12"))
#   add_definitions("-DENABLE_FP8")
#   option(ENABLE_FP8 "ENABLE_FP8" OFF)
#   if(ENABLE_FP8)
#     message("CUDA_VERSION ${CUDA_VERSION_MAJOR}.${CUDA_VERSION_MINOR} is greater or equal than 11.8, enable -DENABLE_FP8 flag")
#   endif()
# endif()

set(CMAKE_MODULE_PATH ${PROJECT_SOURCE_DIR}/cmake/Modules)

option(BUILD_PYT "Build in PyTorch TorchScript class mode" OFF)
if(NOT BUILD_MULTI_GPU)
  option(BUILD_MULTI_GPU "Build project about multi-GPU" OFF)
endif()
if(NOT USE_TRITONSERVER_DATATYPE)
  option(USE_TRITONSERVER_DATATYPE "Build triton backend for triton server" OFF)
endif()
option(BUILD_PY_FFI "Build python ffi" ON)
option(BUILD_TEST "Build tests" OFF)

include(FetchContent)
if (BUILD_TEST)
  FetchContent_Declare(
    repo-cutlass
    GIT_REPOSITORY https://github.com/NVIDIA/cutlass.git
    GIT_TAG        6f47420213f757831fae65c686aa471749fa8d60
    GIT_SHALLOW ON
  )

  set(CUTLASS_ENABLE_HEADERS_ONLY ON CACHE BOOL "Enable only the header library")

  FetchContent_MakeAvailable(repo-cutlass)

  set(CUTLASS_HEADER_DIR ${PROJECT_SOURCE_DIR}/3rdparty/cutlass/include)
  set(CUTLASS_EXTENSIONS_DIR ${PROJECT_SOURCE_DIR}/src/turbomind/cutlass_extensions/include)
endif()

FetchContent_Declare(
  yaml-cpp
  GIT_REPOSITORY https://github.com/jbeder/yaml-cpp.git
  GIT_TAG 0.8.0
)
set(YAML_BUILD_SHARED_LIBS OFF CACHE BOOL "Build static library of yaml-cpp")
FetchContent_MakeAvailable(yaml-cpp)


option(SPARSITY_SUPPORT "Build project with Ampere sparsity feature support" OFF)

option(BUILD_FAST_MATH "Build in fast math mode" ON)

# the environment variable
#   ASAN_OPTIONS=protect_shadow_gap=0,intercept_tls_get_addr=0
# must be set at runtime
# https://github.com/google/sanitizers/issues/1322
if (LMDEPLOY_ASAN_ENABLE)
    add_compile_options($<$<COMPILE_LANGUAGE:CXX>:-fsanitize=address>)
    add_link_options(-fsanitize=address)
endif ()

# notice that ubsan has linker issues for ubuntu < 18.04, see
# https://stackoverflow.com/questions/50024731/ld-unrecognized-option-push-state-no-as-needed
if (LMDEPLOY_UBSAN_ENABLE)
    add_compile_options($<$<COMPILE_LANGUAGE:CXX>:-fsanitize=undefined>)
    add_link_options(-fsanitize=undefined)
endif ()

if(BUILD_MULTI_GPU)
  message(STATUS "Add DBUILD_MULTI_GPU, requires MPI and NCCL")
  add_definitions("-DBUILD_MULTI_GPU")
  set(CMAKE_MODULE_PATH ${PROJECT_SOURCE_DIR}/cmake/Modules)
  find_package(MPI REQUIRED)
  find_package(NCCL REQUIRED)
  set(CMAKE_MODULE_PATH "") # prevent the bugs for pytorch building
endif()

if(BUILD_PYT)
  if(DEFINED ENV{NVIDIA_PYTORCH_VERSION})
    if($ENV{NVIDIA_PYTORCH_VERSION} VERSION_LESS "20.03")
      message(FATAL_ERROR "NVIDIA PyTorch image is too old for TorchScript mode.")
    endif()
    if($ENV{NVIDIA_PYTORCH_VERSION} VERSION_EQUAL "20.03")
      add_definitions(-DLEGACY_THS=1)
    endif()
  endif()
endif()

if(USE_TRITONSERVER_DATATYPE)
  message("-- USE_TRITONSERVER_DATATYPE")
  add_definitions("-DUSE_TRITONSERVER_DATATYPE")
endif()

set(CXX_STD "17" CACHE STRING "C++ standard")
# enable gold linker for binary and .so
set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -fuse-ld=gold")
set(CMAKE_SHARED_LINKER_FLAGS "${CMAKE_SHARED_LINKER_FLAGS} -fuse-ld=gold")
set(CUDA_PATH ${CUDA_TOOLKIT_ROOT_DIR})

set(TF_PATH "" CACHE STRING "TensorFlow path")
set(CUSPARSELT_PATH "" CACHE STRING "cuSPARSELt path")

if((BUILD_TF OR BUILD_TF2) AND NOT TF_PATH)
  message(FATAL_ERROR "TF_PATH must be set if BUILD_TF or BUILD_TF2 (=TensorFlow mode) is on.")
endif()

list(APPEND CMAKE_MODULE_PATH ${CUDA_PATH}/lib64)

# profiling
option(USE_NVTX "Whether or not to use nvtx" ON)
if(USE_NVTX)
  message(STATUS "NVTX is enabled.")
  add_definitions("-DUSE_NVTX")
endif()

# setting compiler flags
set(CMAKE_C_FLAGS    "${CMAKE_C_FLAGS}")
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CUDA_FLAGS "${CMAKE_CUDA_FLAGS} -Xcompiler -Wall -ldl") # -Xptxas -v

# TODO: build for sm_72 & sm_87 on aarch64 platform (Jetson devices)
if (NOT CMAKE_CUDA_ARCHITECTURES)
  set(CMAKE_CUDA_ARCHITECTURES 70-real 75-real)
  if (${CUDA_VERSION} VERSION_GREATER_EQUAL "11")
    list(APPEND CMAKE_CUDA_ARCHITECTURES 80-real)
  endif ()
  if (${CUDA_VERSION} VERSION_GREATER_EQUAL "11.1")
    list(APPEND CMAKE_CUDA_ARCHITECTURES 86-real)
  endif ()
  if (${CUDA_VERSION} VERSION_GREATER_EQUAL "11.8")
    list(APPEND CMAKE_CUDA_ARCHITECTURES 89-real 90-real)
  endif ()
  if (MSVC)
    list(REMOVE_ITEM CMAKE_CUDA_ARCHITECTURES 80-real 90-real)
  endif ()
endif ()

message(STATUS "Building with CUDA archs: ${CMAKE_CUDA_ARCHITECTURES}")

set(CMAKE_CUDA_RUNTIME_LIBRARY Shared)
set(CMAKE_C_FLAGS_DEBUG    "${CMAKE_C_FLAGS_DEBUG}    -Wall -O0")
set(CMAKE_CXX_FLAGS_DEBUG  "${CMAKE_CXX_FLAGS_DEBUG}  -Wall -O0")
# set(CMAKE_CUDA_FLAGS_DEBUG "${CMAKE_CUDA_FLAGS_DEBUG} -O0 -G -Xcompiler -Wall  --ptxas-options=-v --resource-usage")
set(CMAKE_CUDA_FLAGS_DEBUG "${CMAKE_CUDA_FLAGS_DEBUG} -O0 -G -Xcompiler -Wall -DCUDA_PTX_FP8_F2FP_ENABLED")

set(CMAKE_CXX_STANDARD "${CXX_STD}")
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_FLAGS "${CMAKE_CUDA_FLAGS} --expt-extended-lambda")
set(CMAKE_CUDA_FLAGS "${CMAKE_CUDA_FLAGS} --expt-relaxed-constexpr")
set(CMAKE_CUDA_FLAGS "${CMAKE_CUDA_FLAGS} --std=c++${CXX_STD} -DCUDA_PTX_FP8_F2FP_ENABLED")

set(CMAKE_CXX_FLAGS_RELEASE        "${CMAKE_CXX_FLAGS_RELEASE}        -O3")
set(CMAKE_CXX_FLAGS_RELWITHDEBINFO "${CMAKE_CXX_FLAGS_RELWITHDEBINFO} -O3")
# set(CMAKE_CUDA_FLAGS_RELEASE "${CMAKE_CUDA_FLAGS_RELEASE} -Xcompiler -O3 --ptxas-options=--verbose")
set(CMAKE_CUDA_FLAGS_RELEASE        "${CMAKE_CUDA_FLAGS_RELEASE}        -Xcompiler -O3 -DCUDA_PTX_FP8_F2FP_ENABLED")
set(CMAKE_CUDA_FLAGS_RELWITHDEBINFO "${CMAKE_CUDA_FLAGS_RELWITHDEBINFO} -Xcompiler -O3 -DCUDA_PTX_FP8_F2FP_ENABLED")

if(BUILD_FAST_MATH)
    set(CMAKE_CUDA_FLAGS_RELEASE        "${CMAKE_CUDA_FLAGS_RELEASE}        --use_fast_math")
    set(CMAKE_CUDA_FLAGS_RELWITHDEBINFO "${CMAKE_CUDA_FLAGS_RELWITHDEBINFO} --use_fast_math")
    message("Release build CUDA flags: ${CMAKE_CUDA_FLAGS_RELEASE}")
endif()

set(CMAKE_ARCHIVE_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
set(CMAKE_LIBRARY_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/lib)
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY ${CMAKE_BINARY_DIR}/bin)

set(COMMON_HEADER_DIRS
  ${PROJECT_SOURCE_DIR}
  ${CUDA_PATH}/include
  ${CUTLASS_HEADER_DIR}
)
message("-- COMMON_HEADER_DIRS: ${COMMON_HEADER_DIRS}")

set(COMMON_LIB_DIRS
  ${CUDA_PATH}/lib64
)

if (SPARSITY_SUPPORT)
  list(APPEND COMMON_HEADER_DIRS ${CUSPARSELT_PATH}/include)
  list(APPEND COMMON_LIB_DIRS ${CUSPARSELT_PATH}/lib64)
  add_definitions(-DSPARSITY_ENABLED=1)
endif()

if(BUILD_TF)
  list(APPEND COMMON_HEADER_DIRS ${TF_PATH}/include)
  list(APPEND COMMON_LIB_DIRS ${TF_PATH})
  add_definitions(-D_GLIBCXX_USE_CXX11_ABI=0)
endif()

if(BUILD_TF2)
  list(APPEND COMMON_HEADER_DIRS ${TF_PATH}/include)
  list(APPEND COMMON_LIB_DIRS ${TF_PATH})
  add_definitions(-D_GLIBCXX_USE_CXX11_ABI=1)
endif()

set(PYTHON_PATH "python" CACHE STRING "Python path")
if(BUILD_PYT)
  execute_process(COMMAND ${PYTHON_PATH} "-c" "from __future__ import print_function; import torch; print(torch.__version__,end='');"
                  RESULT_VARIABLE _PYTHON_SUCCESS
                  OUTPUT_VARIABLE TORCH_VERSION)
  if (TORCH_VERSION VERSION_LESS "1.5.0")
      message(FATAL_ERROR "PyTorch >= 1.5.0 is needed for TorchScript mode.")
  endif()
  execute_process(COMMAND ${PYTHON_PATH} "-c" "from __future__ import print_function; import os; import torch;
print(os.path.dirname(torch.__file__),end='');"
                  RESULT_VARIABLE _PYTHON_SUCCESS
                  OUTPUT_VARIABLE TORCH_DIR)
  if (NOT _PYTHON_SUCCESS MATCHES 0)
      message(FATAL_ERROR "Torch config Error.")
  endif()
  list(APPEND CMAKE_PREFIX_PATH ${TORCH_DIR})
  find_package(Torch REQUIRED)
  execute_process(COMMAND ${PYTHON_PATH} "-c" "from __future__ import print_function; from distutils import sysconfig;
print(sysconfig.get_python_inc());"
                  RESULT_VARIABLE _PYTHON_SUCCESS
                  OUTPUT_VARIABLE PY_INCLUDE_DIR)
  if (NOT _PYTHON_SUCCESS MATCHES 0)
      message(FATAL_ERROR "Python config Error.")
  endif()
  list(APPEND COMMON_HEADER_DIRS ${PY_INCLUDE_DIR})
  execute_process(COMMAND ${PYTHON_PATH} "-c" "from __future__ import print_function; import torch;
print(torch._C._GLIBCXX_USE_CXX11_ABI,end='');"
                  RESULT_VARIABLE _PYTHON_SUCCESS
                  OUTPUT_VARIABLE USE_CXX11_ABI)
  message("-- USE_CXX11_ABI=${USE_CXX11_ABI}")
  if (USE_CXX11_ABI)
    set(CMAKE_CUDA_FLAGS_RELWITHDEBINFO "${CMAKE_CUDA_FLAGS_RELWITHDEBINFO} -D_GLIBCXX_USE_CXX11_ABI=1")
    set(CMAKE_CXX_FLAGS_RELWITHDEBINFO "${CMAKE_CXX_FLAGS_RELWITHDEBINFO} -D_GLIBCXX_USE_CXX11_ABI=1")
    set(CMAKE_CUDA_FLAGS_RELEASE "${CMAKE_CUDA_FLAGS_RELEASE} -D_GLIBCXX_USE_CXX11_ABI=1")
    set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -D_GLIBCXX_USE_CXX11_ABI=1")
    set(CMAKE_CUDA_FLAGS_DEBUG "${CMAKE_CUDA_FLAGS_DEBUG} -D_GLIBCXX_USE_CXX11_ABI=1")
    set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} -D_GLIBCXX_USE_CXX11_ABI=1")
  else()
    set(CMAKE_CUDA_FLAGS_RELWITHDEBINFO "${CMAKE_CUDA_FLAGS_RELWITHDEBINFO} -D_GLIBCXX_USE_CXX11_ABI=0")
    set(CMAKE_CXX_FLAGS_RELWITHDEBINFO "${CMAKE_CXX_FLAGS_RELWITHDEBINFO} -D_GLIBCXX_USE_CXX11_ABI=0")
    set(CMAKE_CUDA_FLAGS_RELEASE "${CMAKE_CUDA_FLAGS_RELEASE} -D_GLIBCXX_USE_CXX11_ABI=0")
    set(CMAKE_CXX_FLAGS_RELEASE "${CMAKE_CXX_FLAGS_RELEASE} -D_GLIBCXX_USE_CXX11_ABI=0")
    set(CMAKE_CUDA_FLAGS_DEBUG "${CMAKE_CUDA_FLAGS_DEBUG} -D_GLIBCXX_USE_CXX11_ABI=0")
    set(CMAKE_CXX_FLAGS_DEBUG "${CMAKE_CXX_FLAGS_DEBUG} -D_GLIBCXX_USE_CXX11_ABI=0")
  endif()
endif()

# turn off warnings on windows
if (MSVC)
  foreach(
    flag_var
    CMAKE_CXX_FLAGS
    CMAKE_CXX_FLAGS_DEBUG
    CMAKE_CXX_FLAGS_RELEASE
    CMAKE_CXX_FLAGS_MINSIZEREL
    CMAKE_CXX_FLAGS_RELWITHDEBINFO
    CMAKE_C_FLAGS
    CMAKE_C_FLAGS_DEBUG
    CMAKE_C_FLAGS_RELEASE
    CMAKE_C_FLAGS_MINSIZEREL
    CMAKE_C_FLAGS_RELWITHDEBINFO
    CMAKE_CUDA_FLAGS
    CMAKE_CUDA_FLAGS_DEBUG
    CMAKE_CUDA_FLAGS_RELEASE
    CMAKE_CUDA_FLAGS_MINSIZEREL
    CMAKE_CUDA_FLAGS_RELWITHDEBINFO)
    string(REGEX REPLACE "-Wall" " /W0 " ${flag_var} "${${flag_var}}")
  endforeach()
endif()

if (BUILD_MULTI_GPU)
  list(APPEND COMMON_HEADER_DIRS ${MPI_INCLUDE_PATH})
endif()

if(USE_TRITONSERVER_DATATYPE)
  list(APPEND COMMON_HEADER_DIRS ${PROJECT_SOURCE_DIR}/../repo-core-src/include)
endif()

include_directories(
  ${COMMON_HEADER_DIRS}
)

link_directories(
  ${COMMON_LIB_DIRS}
)

# add_subdirectory(3rdparty)
add_subdirectory(src)
# add_subdirectory(examples)

if(BUILD_TEST)
    add_subdirectory(tests/csrc)
endif()

# install python api
if (BUILD_PY_FFI)
  install(TARGETS _turbomind DESTINATION ${CMAKE_SOURCE_DIR}/lmdeploy/lib)
endif ()

if (MSVC)
  return()
endif ()

# # Mesaure the compile time
option(MEASURE_BUILD_TIME "Measure the build time of each module" OFF)
if (MEASURE_BUILD_TIME)
  set_property(GLOBAL PROPERTY RULE_LAUNCH_COMPILE "${CMAKE_COMMAND} -E time")
  set_property(GLOBAL PROPERTY RULE_LAUNCH_CUSTOM "${CMAKE_COMMAND} -E time")
  set_property(GLOBAL PROPERTY RULE_LAUNCH_LINK "${CMAKE_COMMAND} -E time")
endif()

########################################

add_library(transformer-shared SHARED
  $<TARGET_OBJECTS:DynamicDecodeLayer>
  $<TARGET_OBJECTS:Llama>
  $<TARGET_OBJECTS:LlamaTritonBackend>
  $<TARGET_OBJECTS:TransformerTritonBackend>
  $<TARGET_OBJECTS:activation_kernels>
  $<TARGET_OBJECTS:ban_bad_words>
  $<TARGET_OBJECTS:cublasAlgoMap>
  $<TARGET_OBJECTS:cublasMMWrapper>
  $<TARGET_OBJECTS:cuda_utils>
  $<TARGET_OBJECTS:custom_ar_comm>
  $<TARGET_OBJECTS:custom_ar_kernels>
  $<TARGET_OBJECTS:attention>
  $<TARGET_OBJECTS:decoding_kernels>
  $<TARGET_OBJECTS:gpt_kernels>
  $<TARGET_OBJECTS:logprob_kernels>
  $<TARGET_OBJECTS:logger>
  $<TARGET_OBJECTS:memory_utils>
  $<TARGET_OBJECTS:mpi_utils>
  $<TARGET_OBJECTS:nccl_utils>
  $<TARGET_OBJECTS:nvtx_utils>
  $<TARGET_OBJECTS:anomaly_handler>
  $<TARGET_OBJECTS:sampling_penalty_kernels>
  $<TARGET_OBJECTS:sampling_topk_kernels>
  $<TARGET_OBJECTS:sampling_topp_kernels>
  $<TARGET_OBJECTS:stop_criteria>
  $<TARGET_OBJECTS:tensor>
  $<TARGET_OBJECTS:unfused_attention_kernels>
)

if (BUILD_MULTI_GPU)
target_link_libraries(transformer-shared PUBLIC
  ${MPI_CXX_LIBRARIES}
  ${NCCL_LIBRARIES}
)
endif()

if(USE_NVTX)
target_link_libraries(transformer-shared PUBLIC
  -lnvToolsExt
)
endif()

set_target_properties(transformer-shared PROPERTIES POSITION_INDEPENDENT_CODE ON)
set_target_properties(transformer-shared PROPERTIES CUDA_RESOLVE_DEVICE_SYMBOLS ON)
set_target_properties(transformer-shared PROPERTIES LINKER_LANGUAGE CXX)
target_link_libraries(transformer-shared PUBLIC -lcudart -lcublas -lcublasLt -lcurand)

include(GNUInstallDirs)
set(INSTALL_CONFIGDIR ${CMAKE_INSTALL_LIBDIR}/cmake/TurboMind)

include(CMakePackageConfigHelpers)
configure_package_config_file(
  ${CMAKE_CURRENT_LIST_DIR}/cmake/TurboMindConfig.cmake.in
  ${CMAKE_CURRENT_BINARY_DIR}/TurboMindConfig.cmake
  INSTALL_DESTINATION ${INSTALL_CONFIGDIR}
)

install(
  FILES
  ${CMAKE_CURRENT_BINARY_DIR}/TurboMindConfig.cmake
  DESTINATION ${INSTALL_CONFIGDIR}
)

install(
  TARGETS
    transformer-shared
  EXPORT
    transformer-shared-targets
  LIBRARY DESTINATION ${CMAKE_INSTALL_PREFIX}/backends/turbomind
  ARCHIVE DESTINATION ${CMAKE_INSTALL_PREFIX}/backends/turbomind
  RUNTIME DESTINATION ${CMAKE_INSTALL_PREFIX}/bin
)

install(
  EXPORT
    transformer-shared-targets
  FILE
    TurboMindTargets.cmake
  DESTINATION
    ${INSTALL_CONFIGDIR}
)

export(
  EXPORT
    transformer-shared-targets
  FILE
    ${CMAKE_CURRENT_BINARY_DIR}/TurboMindTargets.cmake
  NAMESPACE
    TritonCore::
)

export(PACKAGE TurboMind)
