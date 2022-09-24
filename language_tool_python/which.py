# -*- coding: utf-8 -*-

"""Cross-platform which command."""

import os
import sys
from distutils.spawn import find_executable

__all__ = ['which']

WIN_ALLOW_CROSS_ARCH = True

def which(program):
    """Identify the location of an executable file."""
    if os.path.split(program)[0]:
        program_path = find_exe(program)
        if program_path:
            return program_path
    else:
        for path in get_path_list():
            program_path = find_exe(os.path.join(path, program))
            if program_path:
                return program_path
    return None


def is_exe(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _get_path_list():
    return os.environ['PATH'].split(os.pathsep)


if os.name == 'nt':
    def find_exe(program):
        root, ext = os.path.splitext(program)
        if ext:
            if is_exe(program):
                return program
        else:
            for ext in os.environ['PATHEXT'].split(os.pathsep):
                program_path = root + ext.lower()
                if is_exe(program_path):
                    return program_path
        return None

    def get_path_list():
        paths = _get_path_list()
        if WIN_ALLOW_CROSS_ARCH:
            alt_sys_path = os.path.expandvars(r"$WINDIR\Sysnative")
            if os.path.isdir(alt_sys_path):
                paths.insert(0, alt_sys_path)
            else:
                alt_sys_path = os.path.expandvars(r"$WINDIR\SysWOW64")
                if os.path.isdir(alt_sys_path):
                    paths.append(alt_sys_path)
        return paths

else:
    def find_exe(program):
        return program if is_exe(program) else None

    get_path_list = _get_path_list

## v2
def get_java_path():
    java_paths = os.getenv('JAVA_HOME')
    if java_paths:
        if not java_paths.endswith('bin'): java_paths = os.path.join(java_paths, 'bin')
        java_paths += f'{os.pathsep + os.getenv("PATH")}'
    return find_executable('java', path = java_paths)

def get_variable_separator():
    """
    Returns the environment variable separator for the current platform.
    :return: Environment variable separator
    """
    return ';' if sys.platform.startswith('win') else ':'

def get_binary_path(executable: str):
    """
    Searches for a binary named `executable` in the current PATH. If an executable is found, its absolute path is returned
    else None.
    :param executable: Name of the binary
    :return: Absolute path or None
    """
    if 'PATH' not in os.environ: return None
    for directory in os.environ['PATH'].split(get_variable_separator()):
        binary = os.path.abspath(os.path.join(directory, executable))
        if os.path.isfile(binary) and os.access(binary, os.X_OK): return binary
    return None

def main():
    for arg in sys.argv[1:]:
        path = which(arg)
        if path:
            print(path)


if __name__ == '__main__':
    sys.exit(main())
