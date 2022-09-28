import os
import re
import sys
import glob
#import httpx
import locale
import subprocess
import http.client
import urllib.parse
import urllib.request

from typing import List, Tuple, Union, Optional

from language_tool_python.config_file import LanguageToolConfig
from language_tool_python.match import Match
from language_tool_python.which import which, get_java_path
from language_tool_python.logs import logger

JAR_NAMES = [
    'languagetool-server.jar',
    'languagetool-standalone*.jar',  # 2.1
    'LanguageTool.jar',
    'LanguageTool.uno.jar'
]
FAILSAFE_LANGUAGE = 'en'

# https://mail.python.org/pipermail/python-dev/2011-July/112551.html

if os.name == 'nt':
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
else:
    startupinfo = None

class LanguageToolError(Exception):
	pass
	
class ServerError(LanguageToolError):
    pass

class JavaError(LanguageToolError):
    pass

class PathError(LanguageToolError):
    pass

def parse_url(url_str):
    """ Parses a URL string, and adds 'http' if necessary. """
    if 'http' not in url_str:
        url_str = f'http://{url_str}'   

    return urllib.parse.urlparse(url_str).geturl()

def correct(text: str, matches: List[Match]) -> str:
    """Automatically apply suggestions to the text."""
    ltext = list(text)
    matches = [match for match in matches if match.replacements]
    errors = [ltext[match.offset:match.offset + match.errorLength]
              for match in matches]
    correct_offset = 0
    for n, match in enumerate(matches):
        frompos, topos = (correct_offset + match.offset,
                          correct_offset + match.offset + match.errorLength)
        if ltext[frompos:topos] != errors[n]:
            continue
        repl = match.replacements[0]
        ltext[frompos:topos] = list(repl)
        correct_offset += len(repl) - len(errors[n])
    return ''.join(ltext)

def get_language_tool_download_path():
    # Get download path from environment or use default.
    download_path = os.environ.get(
        'LTP_PATH',
        os.path.join(os.path.expanduser("~"), ".cache", "language_tool_python")
    )
    # Make download path, if it doesn't exist.
    os.makedirs(download_path, exist_ok=True)
    return download_path

def get_language_tool_directory():
    """Get LanguageTool directory."""
    download_folder = get_language_tool_download_path()
    assert os.path.isdir(download_folder)
    language_tool_path_list = [
        path for path in
        glob.glob(os.path.join(download_folder, 'LanguageTool*'))
        if os.path.isdir(path)
    ]

    if not len(language_tool_path_list):
        raise FileNotFoundError(f'LanguageTool not found in {download_folder}.')

    return max(language_tool_path_list)


def get_server_cmd(
    port: int = None, 
    config: LanguageToolConfig = None,
    java_options: Optional[List[str]] = None,
    options: Optional[List[str]] = None,
) -> Union[List[str], str]:
    java_path, jar_path = get_jar_info()
    cmd = [
        java_path
    ]
    if java_options:
        logger.info(f'Adding Java options: {java_options}')
        cmd.extend(java_options)
    cmd.extend(
        ['-cp', jar_path, 'org.languagetool.server.HTTPServer']
    )
    if port is not None: cmd += ['-p', str(port)]
    if config is not None: cmd += ['--config', config.path]
    if options is not None: 
        logger.info(f'Adding options: {options}')
        cmd += options
    return cmd


def get_jar_info() -> Tuple[str, str]:
    java_path = get_java_path()
    # which('java')
    if not java_path:
        raise JavaError("can't find Java")
    dir_name = get_language_tool_directory()
    jar_path = None
    for jar_name in JAR_NAMES:
        for jar_path in glob.glob(os.path.join(dir_name, jar_name)):
            if os.path.isfile(jar_path):
                break
        else:
            jar_path = None
        if jar_path:
            break
    else:
        raise PathError("can't find languagetool-standalone in {!r}"
                        .format(dir_name))
    return java_path, jar_path


def get_locale_language():
    """Get the language code for the current locale setting."""
    return locale.getlocale()[0] or locale.getdefaultlocale()[0]