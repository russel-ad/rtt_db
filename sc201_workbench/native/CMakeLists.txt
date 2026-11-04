# Included at the end of the unmodified product's CMake directory via
# InjectWorkbench.cmake. The product retains its normal root/crypto context.
set(SC201_HOST_SOURCE "${CMAKE_SOURCE_DIR}")
if(NOT Qt5Core_VERSION_STRING STREQUAL "5.15.2")
  message(FATAL_ERROR "The RTT workbench requires the host's Qt 5.15.2 runtime")
endif()
include("${RTT_WORKBENCH_SOURCE}/WorkbenchOverlay.cmake")

# Compile the existing workbench itself, excluding only its standalone main/RC.
# Its frozen protocol, TLS, workflows, permissions and UI remain the source of truth.
get_target_property(host_sources sc201_host SOURCES)
set(embedded_sources)
foreach(source IN LISTS host_sources)
  if(source MATCHES "(^|/)(main|HostLog)\\.cpp$" OR source MATCHES "\\.rc$")
    continue()
  endif()
  if(NOT IS_ABSOLUTE "${source}")
    set(source "${SC201_HOST_SOURCE}/${source}")
  endif()
  get_filename_component(name "${source}" NAME)
  if(name IN_LIST RTT_WORKBENCH_OVERLAY_FILES)
    set(source "${RTT_WORKBENCH_OVERLAY}/${name}")
  endif()
  list(APPEND embedded_sources "${source}")
endforeach()
add_library(sc201_workbench SHARED
  "${RTT_WORKBENCH_SOURCE}/bridge.cpp"
  "${RTT_WORKBENCH_SOURCE}/batch_ota.h" "${RTT_WORKBENCH_SOURCE}/batch_ota.cpp"
  "${RTT_WORKBENCH_SOURCE}/batch_ota_task.h" "${RTT_WORKBENCH_SOURCE}/batch_ota_task.cpp"
  "${RTT_WORKBENCH_SOURCE}/batch_ota_panel.h" "${RTT_WORKBENCH_SOURCE}/batch_ota_panel.cpp"
  "${RTT_WORKBENCH_SOURCE}/ble_speed.h" "${RTT_WORKBENCH_SOURCE}/ble_speed.cpp"
  "${RTT_WORKBENCH_SOURCE}/ota_tuning.h"
  "${RTT_WORKBENCH_SOURCE}/ota_att_plan.h"
  "${RTT_WORKBENCH_SOURCE}/ota_drive.h" "${RTT_WORKBENCH_SOURCE}/ota_drive.cpp"
  "${RTT_WORKBENCH_SOURCE}/host_log_adapter.cpp" ${embedded_sources})
target_include_directories(sc201_workbench PRIVATE "${RTT_WORKBENCH_OVERLAY}" "${SC201_HOST_SOURCE}/src" "${RTT_WORKBENCH_SOURCE}")
target_link_libraries(sc201_workbench PRIVATE
  Qt5::Core Qt5::Gui Qt5::Widgets Qt5::Bluetooth Qt5::Network
  sc201_host_workflows)
if(MSVC)
  target_compile_options(sc201_workbench PRIVATE /utf-8 /bigobj /W4)
  target_compile_definitions(sc201_workbench PRIVATE
    _WIN32_WINNT=0x0A00 WINVER=0x0A00 NOMINMAX)
  target_link_libraries(sc201_workbench PRIVATE WindowsApp crypt32)
endif()

option(RTT_WORKBENCH_BUILD_TESTS "Build isolated batch OTA scheduler/session tests" OFF)
if(RTT_WORKBENCH_BUILD_TESTS)
  find_package(Qt5 REQUIRED COMPONENTS Test)
  set(session_test_sources)
  foreach(source IN LISTS MLINK_HOST_SESSION_SOURCES)
    get_filename_component(name "${source}" NAME)
    if(name IN_LIST RTT_WORKBENCH_OVERLAY_FILES)
      set(source "${RTT_WORKBENCH_OVERLAY}/${name}")
    endif()
    list(APPEND session_test_sources "${source}")
  endforeach()
  add_executable(test_rtt_batch_ota
    "${RTT_WORKBENCH_SOURCE}/../../tests_workbench/test_batch_ota.cpp"
    "${RTT_WORKBENCH_SOURCE}/batch_ota.h" "${RTT_WORKBENCH_SOURCE}/batch_ota.cpp"
    "${RTT_WORKBENCH_SOURCE}/batch_ota_task.h" "${RTT_WORKBENCH_SOURCE}/batch_ota_task.cpp"
    "${RTT_WORKBENCH_SOURCE}/ble_speed.h" "${RTT_WORKBENCH_SOURCE}/ble_speed.cpp"
    "${RTT_WORKBENCH_SOURCE}/ota_tuning.h"
    "${RTT_WORKBENCH_SOURCE}/ota_att_plan.h"
    "${RTT_WORKBENCH_SOURCE}/ota_drive.h" "${RTT_WORKBENCH_SOURCE}/ota_drive.cpp"
    ${session_test_sources})
  target_include_directories(test_rtt_batch_ota PRIVATE "${RTT_WORKBENCH_OVERLAY}" "${SC201_HOST_SOURCE}/src" "${RTT_WORKBENCH_SOURCE}")
  target_link_libraries(test_rtt_batch_ota PRIVATE Qt5::Test Qt5::Network sc201_host_workflows)
  if(MSVC)
    target_compile_options(test_rtt_batch_ota PRIVATE /utf-8 /bigobj /W4)
    target_compile_definitions(test_rtt_batch_ota PRIVATE _WIN32_WINNT=0x0A00 WINVER=0x0A00 NOMINMAX)
    target_link_libraries(test_rtt_batch_ota PRIVATE WindowsApp)
  endif()
endif()
