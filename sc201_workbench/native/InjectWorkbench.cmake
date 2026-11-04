# CMAKE_PROJECT_sc201_host_INCLUDE injects an additional build target without
# editing or copying the original checkout or its protocol/crypto build files.
function(rtt_add_sc201_workbench)
  include("${RTT_WORKBENCH_SOURCE}/CMakeLists.txt")
endfunction()
cmake_language(DEFER CALL rtt_add_sc201_workbench)
