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
    dependency('libdrm'),
    dependency('libtiff-4'),
    dependency('nlohmann_json'),
    dependency('boost', modules : ['program_options']),
    dependency('threads'),
]

lj92_inc = include_directories('third_party/lj92', is_system : true)

# Round trip test of the DNG writer (tests/dng_roundtrip.sh), not installed.
executable('dng-roundtrip',
           files('tests/dng_roundtrip.cpp', 'src/dng_writer.cpp'),
           include_directories : lj92_inc,
           dependencies : deps)

test('analysis', executable('analysis-test', files('tests/analysis_test.cpp', 'src/analysis.cpp')))
test('controls', executable('controls-test', files('tests/controls_test.cpp', 'src/control_codec.cpp'),
                            dependencies : [dependency('libcamera'), dependency('nlohmann_json')]))
test('look', executable('look-test', files('tests/look_test.cpp', 'src/look.cpp'), dependencies : dependency('threads')))
# Development tool: an engine look on any picture (tools/look_apply.py), not installed.
executable('look-apply', files('tests/look_apply.cpp', 'src/look.cpp'),
           dependencies : [dependency('threads'), dependency('nlohmann_json')])

executable('pycam-engine',
           files(
               'src/main.cpp',
               'src/engine.cpp',
               'src/ipc_server.cpp',
               'src/capture_writer.cpp',
               'src/control_codec.cpp',
               'src/display.cpp',
               'src/dng_writer.cpp',
               'src/focus_aids.cpp',
               'src/analysis.cpp',
               'src/look.cpp',
           ),
           # Vendored code (see its README): a system include, its warnings are not ours.
           include_directories : lj92_inc,
           dependencies : deps,
           install : true)
