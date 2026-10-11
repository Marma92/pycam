project('pycam-engine', 'cpp',
        version : '0.1.0',
        meson_version : '>= 1.0',
        default_options : ['cpp_std=c++20', 'warning_level=2', 'buildtype=release'])

rpicam_app_dep = dependency('rpicam_app')
# The rpicam-apps headers include each other as "core/...", "image/..." : add their root,
# as a system include so that their warnings do not drown ours.
rpicam_includedir = rpicam_app_dep.get_variable(pkgconfig : 'includedir') / 'rpicam-apps'
rpicam_dep = declare_dependency(dependencies : rpicam_app_dep,
                                compile_args : ['-isystem', rpicam_includedir])

deps = [
    rpicam_dep,
    dependency('libcamera'),
    dependency('nlohmann_json'),
    dependency('boost', modules : ['program_options']),
    dependency('threads'),
]

executable('pycam-engine',
           files(
               'src/main.cpp',
               'src/engine.cpp',
               'src/ipc_server.cpp',
               'src/capture_writer.cpp',
               'src/control_codec.cpp',
               'src/analysis.cpp',
           ),
           dependencies : deps,
           install : true)
