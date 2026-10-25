from setuptools import find_packages, setup

setup(
    name='TorrentAPI', version='1.0.1',
    description='Authenticated HTTP torrent API for Deluge 2',
    author='Garrett Marsh', author_email='gmarsh0218@gmail.com',
    license='MIT',
    url='https://github.com/GMarsh0218/deluge-torrent-api',
    packages=find_packages(), python_requires='>=3.8',
    entry_points={'deluge.plugin.core': ['TorrentAPI = deluge_torrentapi:CorePlugin']},
    include_package_data=True, zip_safe=True,
)
