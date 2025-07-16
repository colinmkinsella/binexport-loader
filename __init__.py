from datetime import datetime
from pathlib import Path
from semver import VersionInfo
from typing import Optional
from zipfile import BadZipFile, ZipFile

import binaryninja
import json
import os
import platform
import shutil
import sys
import requests


# Plugin details
PLUGIN_NAME = "BinExport"

# Repository details
REPO_OWNER = "colinmkinsella"
REPO_NAME = "binexport"

PLUGIN_DIR = Path(binaryninja.user_plugin_path())
RELEASE_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"
TEMP_DIR = Path(binaryninja.user_plugin_path()) / "temp"


class BinExportVersion:
    def __init__(self, version_string):
        major_version, commit_date = version_string.split('-')
        major_version = major_version.replace("BinExport", "")
        self.major_version = int(major_version)

        self.commit_date = datetime.strptime(commit_date, "%Y%m%d").date()

    def __lt__(self, other):
        if self.major_version != other.major_version:
            return self.major_version > other.major_version
        return self.commit_date > other.commit_date

    def __eq__(self, other):
        return self.major_version == other.major_version and self.commit_date == other.major_version

    def __repr__(self):
        return f"{self.major_version}-{self.commit_date}"


class BinaryNinjaVersion:
    def __init__(self, version_string):
        self.version = str(version_string.split('-', 1)[1])
        ver_num = self.version

        if self.version.endswith('stable'):
            self.channel = "stable"
            ver_num = ver_num[1:].replace("-stable", "")
        elif self.version.startswith('dev'):
            self.channel = "dev"
            ver_num = ver_num.replace("dev-", "")

        ver_info = ver_num.split(".")
        self.major_version = int(ver_info[0])
        self.minor_version = int(ver_info[1])
        self.build_version = int(ver_info[2])

    def __eq__(self, other):
        return self.channel == other.channel and self.major_version == other.major_version and self.minor_version == self.minor_version and self.build_version == self.build_version

    def __lt__(self, other):
        if self.major_version != other.major_version:
            return self.major_version > other.major_version
        if self.minor_version != other.minor_version:
            return self.minor_version > other.minor_version
        return self.build_version > other.build_version

    def __repr__(self):
        return self.version


class PluginVersion:
    def __init__(self, asset: dict):
        (filename_noext, _) = os.path.splitext(asset["name"])
        versions = filename_noext.split('_')
        if len(versions) != 4:
            binaryninja.log.log_error(
                f"incorrect plugin filename: {asset['name']}")
            return

        self.asset = asset
        self.filename = asset["name"]
        self.binexport_version = BinExportVersion(versions[0])
        self.binaryninja_version = BinaryNinjaVersion(versions[1])
        self.plugin_version = VersionInfo.parse(versions[2].split('-')[1])

        os_arch = versions[3].split('-')
        if len(os_arch) != 2:
            binaryninja.log.log_error(
                f"incorrect os/architecture string in filename: {os_arch}")
        self.os = os_arch[0]
        self.arch = os_arch[1]

    def __lt__(self, other):
        if self.plugin_version != other.plugin_version:
            return self.plugin_version > other.plugin_version
        elif self.binexport_version != other.binexport_version:
            return self.binaryninja_version > other.binexport_version
        else:
            return self.binaryninja_version > self.binaryninja_version

    def __eq__(self, other):
        return self.plugin_version == other.plugin_version and self.binexport_version.major_version == other.binexport_version.major_version and self.binexport_version.commit_date == other.binexport_version.commit_date

    def __repr__(self):
        return f"Plugin Version: {self.plugin_version} BinaryNinja Version: {self.binaryninja_version} BinExport Version: {self.binexport_version}"

    def get_download_url(self) -> str:
        return self.asset["browser_download_url"]

    def get_sha256_digest(self) -> str:
        digest = str(self.asset["digest"])
        hash_type, hash_digest = digest.split(":")
        if hash_type == "sha256":
            return hash_digest
        else:
            return ""


class PluginVersions:
    def __init__(self):
        self.plugin_versions = []

    def add_plugin(self, asset: dict):
        plugin_version = PluginVersion(asset)
        self.plugin_versions.append(plugin_version)

    def find_compatible_plugin(self, os_name, arch, binaryninja_version) -> Optional[PluginVersion]:
        for plugin in self.plugin_versions:
            print(
                f"os={plugin.os} arch={plugin.arch} binaryninja_version={plugin.binaryninja_version}")
            if plugin.os == os_name and plugin.arch == arch and str(plugin.binaryninja_version) == binaryninja_version:
                return plugin
        return None

    def sort_by_newest(self):
        self.plugin_versions = sorted(self.plugin_versions)


def create_empty_temp_dir() -> Optional[Path]:
    """
    Creates an empty temporary directory in the user plugin path. It will delete any existing
    files, symlinks, or directories in the directory.

    Returns: Returns the temp directory Path on success otherwise None.
    """
    try:
        if not TEMP_DIR.exists():
            os.mkdir(TEMP_DIR)
        else:
            for item in TEMP_DIR.glob("*"):
                if item.is_file() or item.is_symlink():
                    item.unlink()
                elif item.is_dir():
                    item.rmdir()

        return TEMP_DIR
    except Exception as e:
        binaryninja.log.log_error(
            f"failed to create empty temp directory: {e}")
        return None


def create_plugin_dir() -> Optional[Path]:
    """
    Creates the plugin directory where this loader plugin saves and registers the binary plugin.

    Returns: Returns the plugin directory Path on success otherwise None.
    """
    try:
        if not PLUGIN_DIR.exists():
            os.mkdir(PLUGIN_DIR)
        return PLUGIN_DIR
    except Exception as e:
        binaryninja.log.log_error(
            f"failed to create empty plugin directory: {e}")
        return None


def download_plugin(file: Path, source_plugin: PluginVersion) -> bool:
    """
    Attempts to download the given plugin from the GitHub workflow release assets.

    Args:
        file (Path): The path to download the file to.
        source_plugin: The PluginVersion to download from.

    Returns: True on success and false on Failure.
    """
    try:
        response = requests.get(
            source_plugin.get_download_url(), stream=True, timeout=30)

        if response.status_code != 200:
            binaryninja.log.log_error(
                "failed to connect to the plugin release asset download url: "
                f"{source_plugin.get_download_url()}"
            )
            return False

        with open(file, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return True
    except Exception as e:
        binaryninja.log.log_error(
            f"failed to download plugin: {file} - {e}")
        return False


def extract_and_remove_zip(zip_file: Path, zip_dir: Path) -> Optional[list[str]]:
    """
    Extracts the zip file at the given path and removes it if successful.

    Args:
        zip_file (Path): The path to the zip file to extract and then delete.
        zip_dir (Path): The directory where the file(s) will be extracted.

    Returns: Returns a list of files extracted or None.
    """
    try:
        list_of_files = list[str]
        with ZipFile(zip_file, 'r') as zip_ref:
            list_of_files = zip_ref.namelist()
            zip_ref.extractall(path=str(zip_dir))
        os.remove(zip_file)
        return list_of_files
    except BadZipFile:
        binaryninja.log.log_error(
            f"'{zip_file}' is not a valid zip file")
        return None
    except FileNotFoundError:
        binaryninja.log.log_error(
            f"zip file not found at '{zip_file}'")
        return None
    except Exception as e:
        binaryninja.log.log_error(
            f"an unexpected error occurred while extracting '{zip_file}': {e}")
        return None


def get_os_name() -> str:
    """
    Gets the Operating System Name in a format compatible with the Github Workflow asset name.

    Returns: The name of the Operating System for this host.
    """
    platform_name = sys.platform.lower()
    os_name = ""

    if platform_name.startswith("win"):
        os_name = "Windows"
    elif platform_name.startswith("linux"):
        os_name = "Linux"
    elif platform_name.startswith("darwin"):
        os_name = "macOS"
    else:
        binaryninja.log.log_error("unsupported platform for plugin")

    return os_name


def get_arch() -> str:
    """
    Gets the Architecture string in a format compatible with the Github Workflow asset name.

    Returns: The name of the Architecture for this host.
    """
    machine_type = platform.machine()
    arch = ""

    if machine_type in ('x86_64', 'AMD64'):
        arch = 'X64'
    elif machine_type in ('arm64', 'aarch64'):
        arch = 'ARM64'
    else:
        binaryninja.log.log_error("unsupported architecture for plugin")

    return arch


def remove_temp_dir() -> bool:
    """
    Removes the temp directory.

    Returns: Returns True on success, otherwise False.
    """
    try:
        for item in TEMP_DIR.glob("*"):
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                item.rmdir()
        return True
    except Exception as e:
        binaryninja.log.log_error(
            f"failed to remove temp directory: {e}")
        return False


# Function that determines whether Binary Ninja version is supported (returns None if not, according file name if yes)


def is_version_supported(files):
    # Get current Binary Ninja version
    version_numbers = binaryninja.core_version().split()[
        0].split('-')[0].split('.')
    major, minor, build = map(int, version_numbers)
    dev_file = None

    # Loop through files for current platform and see if our version is supported by any
    for entry in files:
        min_ver, max_ver, file = entry

        # first check all non dev versions (there might be specific binary for specific dev versions so use that and if none found then we can use binary for all dev versions)
        if (min_ver != 'DEV' and max_ver != 'DEV'):
            min_parts = min_ver.split('.')
            max_parts = max_ver.split('.')

            major_match = (major >= int(
                min_parts[0]) and major <= int(max_parts[0]))
            minor_match = (minor >= int(
                min_parts[1]) and minor <= int(max_parts[1]))
            build_match = (build >= int(
                min_parts[2]) and build <= int(max_parts[2]))

            if major_match and minor_match and build_match:
                return file
        else:
            dev_file = file

    # If we are on dev, check if there is a file for all dev versions
    if ('-dev' in binaryninja.core_version() and dev_file != None and len(dev_file) > 0):
        return dev_file

    return None

# Function that determines whether system is supported


def is_system_supported(file_name):
    return file_name != None and len(file_name) > 0

# Function that determines whether native_plugins_data folder exists


def data_folder_exists():
    return os.path.isdir(os.path.join(binaryninja.user_plugin_path(), 'native_plugins_data'))

# Function that determines whether current_native_plugin_data file exists


def data_file_exists():
    return os.path.isfile(os.path.join(binaryninja.user_plugin_path(), 'native_plugins_data', PLUGIN_NAME + '.data'))

# Function that determines whether temp folder exists


def temp_folder_exists():
    return os.path.isdir(os.path.join(binaryninja.user_plugin_path(), 'temp'))

# Function that reads current_native_plugin_data file


def read_data_file():
    with open(os.path.join(binaryninja.user_plugin_path(), 'native_plugins_data', PLUGIN_NAME + '.data'), 'r') as f:
        return f.read().splitlines()

# Function that writes to current_native_plugin_data file


def write_data_file(version, hash, file_name):
    with open(os.path.join(binaryninja.user_plugin_path(), 'native_plugins_data', PLUGIN_NAME + '.data'), 'w') as f:
        f.write(version + '\n' + hash + '\n' + file_name)

# Function that deletes file from current_native_plugin_data


def delete_data_file():
    path = os.path.join(binaryninja.user_plugin_path(),
                        'native_plugins_data', PLUGIN_NAME + '.data')
    if os.path.isfile(path):
        try:
            os.remove(path)
        except Exception as error:
            return path
    return True

# Function that calculates hash of file


def calculate_hash(file_path):
    import hashlib
    hash = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(4096), b""):
            hash.update(chunk)
    return hash.hexdigest()

# Function that downloads file


def download_file(file_url, file_name):
    response = requests.get(file_url)
    if response.status_code == 200:
        with open(os.path.join(binaryninja.user_plugin_path(), file_name), 'wb') as f:
            f.write(response.content)
        return True
    else:
        return False

# Function that downloads file to temp directory


def download_file_to_temp(file_url, file_name):
    response = requests.get(file_url)
    if response.status_code == 200:
        with open(os.path.join(binaryninja.user_plugin_path(), 'temp', file_name), 'wb') as f:
            f.write(response.content)
        return True
    else:
        return False

# Function that deletes file


def delete_file(path) -> bool:
    try:
        os.remove(path)
    except FileNotFoundError:
        binaryninja.log.log_error(f"{path} not found")
        return False
    except PermissionError:
        binaryninja.log.log_error(
            f"permission error while trying to delete {path}")
        return False
    except OSError as e:
        binaryninja.log.log_error(
            f"unexpected OS error while deleting '{path}': {e}")
        return False

    return True

# Function that deletes file from temp directory


# def delete_file_from_temp(file_name):
#    path = os.path.join(binaryninja.user_plugin_path(), 'temp', file_name)
#    if os.path.isfile(path):
#        try:
#            os.remove(path)
#        except FileNotFoundError:
#            print(f"Error: File '{file_to_delete}' not found.")
#            return False
#        except PermissionError:
#            print(f"Error: Permission denied to delete '{file_to_delete}'.")
#            return False
#        except OSError as e:
#            print(
#                f"An unexpected OS error occurred while deleting '{file_to_delete}': {e}")
#            return path
#    return True

# Function that determines whether plugin is installed (for current Binary Ninja version)


def is_plugin_installed(file_name):
    return os.path.isfile(os.path.join(binaryninja.user_plugin_path(), file_name))

# Function that alerts user


def alert_user(description):
    binaryninja.interaction.show_message_box('{} (Binexport plugin loader)'.format(
        PLUGIN_NAME), description, binaryninja.enums.MessageBoxButtonSet.OKButtonSet, binaryninja.enums.MessageBoxIcon.InformationIcon)


# Function that does the actual work


def check_for_updates():
    os_name = get_os_name()
    arch = get_arch()

    ver = binaryninja.core_version_info()
    binaryninja_version = ""
    if ver.channel == "dev":
        binaryninja_version = f"dev-{ver.major}.{ver.minor}.{ver.build}"
    else:
        binaryninja_version = f"v{ver.major}.{ver.minor}.{ver.build}-stable"

    plugin_versions = PluginVersions()

    response = requests.get(RELEASE_URL, timeout=10)
    if response.status_code != 200:
        binaryninja.log.log_error(
            f"failed to connect to the plugin release url: {RELEASE_URL}")
        return

    release = response.json()
    assets = release.get("assets")
    for asset in assets:
        plugin_versions.add_plugin(asset)

    plugin_versions.sort_by_newest()

    plugin = plugin_versions.find_compatible_plugin(
        os_name, arch, binaryninja_version)
    if plugin is None:
        alert_user(
            f"Failed to find a compatible version of the plugin for your version of Binary Ninja {binaryninja_version}.")
        return

    temp_dir = create_empty_temp_dir()
    if temp_dir is None:
        return

    temp_plugin_file = temp_dir / plugin.filename
    if not download_plugin(temp_plugin_file, plugin):
        return

    download_hash = calculate_hash(temp_plugin_file)
    asset_hash = plugin.get_sha256_digest()
    if download_hash != asset_hash:
        os.remove(temp_plugin_file)
        error_msg = (
            f": downloaded plugin zip hash {download_hash} did not match asset hash from"
            "and github {asset_hash}: \n\t{plugin.get_download_url()}"
        )
        binaryninja.log.log_error(error_msg)
        return

    extracted_files = extract_and_remove_zip(temp_plugin_file, temp_dir)
    if extracted_files is None:
        binaryninja.log.log_error(
            f"no files extracted from {temp_plugin_file}")
        return

    try:
        for file in extracted_files:
            shutil.move(temp_dir / file, PLUGIN_DIR / file)
    except Exception as e:
        binaryninja.log.log_error(
            f"failed to move plugin file(s) from temp directory to plugin directory: {e}")
        return

    remove_temp_dir()

    alert_user(
        f"""
        {PLUGIN_NAME} updated to {plugin.binexport_version} -
        BinaryNinja {plugin.binaryninja_version}.
        Please restart to load.
        """)


class Updater(binaryninja.BackgroundTaskThread):
    def __init__(self):
        binaryninja.BackgroundTaskThread.__init__(
            self, f"BinExport Plugin Loader - checking for updates on: {RELEASE_URL}", True)

    def run(self):
        check_for_updates()


obj = Updater()
obj.start()
