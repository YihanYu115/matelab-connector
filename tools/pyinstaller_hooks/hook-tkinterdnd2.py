"""Collect the native TkDND libraries bundled with tkinterdnd2."""

from PyInstaller.utils.hooks import collect_data_files

datas = collect_data_files("tkinterdnd2")
